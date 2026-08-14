#!/usr/bin/awk -f
# vault-recovery-manifest-schema.awk - STRICT full-schema validator for
# schema-version-1 vault-recovery manifests (POSIX awk; the pinned rclone
# image has busybox awk only — no python3, no jq).
#
# This is the shell-side twin of the AUTHORITATIVE Python validator
# (validate_manifest_schema in scripts/vault_recovery_core.py). It enforces
# the exact same contract: every block, key, type, and digest format is
# checked, UNKNOWN KEYS anywhere are rejected, and the doctor metadata
# (required_checks / check_counts) is validated exactly like the restore
# core validates it — a generation that passes this gate always restores in
# the Python core, and one the core would refuse is refused here too.
# Well-formedness is a subset of this check (full RFC 8259 grammar).
#
# Input: the manifest text (file argument or stdin).
#
# On success (exit 0) it prints the extracted values the shell steps need,
# one per line, TAB-separated:
#   schema_version\t<integer>
#   generation_id\t<id>
#   entries_digest\t<tree>\t<sha256>
#
# Exit 1 with a diagnostic on stderr for any violation (malformed JSON,
# unknown/missing key, wrong type, invalid digest, invalid doctor metadata).
#
# POSIX-awk safe: only substr/length/index/match-style operations, explicit
# local variables via the parameter list, recursion bounded by the
# manifest's shallow nesting. Duplicate keys are rejected (the exporter
# never emits them; a document with duplicate keys is suspect even though
# Python dict semantics would silently keep the last value).

function die(msg) {
    print "vault-recovery-manifest-schema: invalid manifest: " msg > "/dev/stderr"
    exit 1
}

function peek() {
    return substr(buf, pos, 1)
}

function advance() {
    pos = pos + 1
}

function skip_ws() {
    c = peek()
    while (pos <= len && (c == " " || c == "\t" || c == "\n" || c == "\r")) {
        pos = pos + 1
        c = peek()
    }
}

function is_hex(ch) {
    return (ch >= "0" && ch <= "9") || (ch >= "a" && ch <= "f") || (ch >= "A" && ch <= "F")
}

function parse_string(   start, c, e, k) {
    # Called with buf[pos] == '"'. Consumes the full string literal and
    # returns the RAW text between the quotes (escapes are NOT unescaped;
    # every value the shell matches is plain ASCII, so raw comparison is
    # fail-closed for anything exotic).
    start = pos
    pos = pos + 1
    while (pos <= len) {
        c = peek()
        if (c == "\\") {
            pos = pos + 1
            if (pos > len) die("unterminated string escape")
            e = peek()
            if (e == "u") {
                pos = pos + 1
                for (k = 1; k <= 4; k = k + 1) {
                    if (pos > len || !is_hex(peek())) die("invalid \\u escape")
                    pos = pos + 1
                }
            } else if (e != "\"" && e != "\\" && e != "/" && e != "b" && e != "f" && e != "n" && e != "r" && e != "t") {
                die("invalid string escape")
            } else {
                pos = pos + 1
            }
        } else if (c == "\"") {
            pos = pos + 1
            return substr(buf, start + 1, pos - start - 2)
        } else if (c < " ") {
            die("control character in string")
        } else {
            pos = pos + 1
        }
    }
    die("unterminated string")
}

function parse_number(   start) {
    # Called with buf[pos] == '-' or a digit. Captures the raw number text.
    start = pos
    if (peek() == "-") pos = pos + 1
    if (pos > len) die("truncated number")
    if (peek() == "0") {
        pos = pos + 1
    } else if (peek() >= "1" && peek() <= "9") {
        while (pos <= len && peek() >= "0" && peek() <= "9") pos = pos + 1
    } else {
        die("malformed number")
    }
    if (pos <= len && peek() == ".") {
        pos = pos + 1
        if (pos > len || !(peek() >= "0" && peek() <= "9")) die("malformed number fraction")
        while (pos <= len && peek() >= "0" && peek() <= "9") pos = pos + 1
    }
    if (pos <= len && (peek() == "e" || peek() == "E")) {
        pos = pos + 1
        if (pos <= len && (peek() == "+" || peek() == "-")) pos = pos + 1
        if (pos > len || !(peek() >= "0" && peek() <= "9")) die("malformed number exponent")
        while (pos <= len && peek() >= "0" && peek() <= "9") pos = pos + 1
    }
    last_num = substr(buf, start, pos - start)
}

function parse_literal(word) {
    wlen = length(word)
    if (substr(buf, pos, wlen) == word) {
        pos = pos + wlen
    } else {
        die("malformed literal")
    }
}

# ---------------------------------------------------------------------------
# Schema tables (mirror the Python core's frozensets exactly)
# ---------------------------------------------------------------------------

BEGIN {
    ALLOW["top", "convergence"] = 1
    ALLOW["top", "created_at_utc"] = 1
    ALLOW["top", "doctor"] = 1
    ALLOW["top", "exporter"] = 1
    ALLOW["top", "generation_id"] = 1
    ALLOW["top", "phase"] = 1
    ALLOW["top", "remote"] = 1
    ALLOW["top", "schema_version"] = 1
    ALLOW["top", "sources"] = 1
    ALLOW["top", "trees"] = 1
    ALLOW["remote", "note"] = 1
    ALLOW["remote", "uploaded"] = 1
    ALLOW["sources", "gbrain_state_dir"] = 1
    ALLOW["sources", "vault_dir"] = 1
    ALLOW["tree", "bytes"] = 1
    ALLOW["tree", "dirs"] = 1
    ALLOW["tree", "entries"] = 1
    ALLOW["tree", "entries_digest"] = 1
    ALLOW["tree", "entries_file"] = 1
    ALLOW["tree", "files"] = 1
    ALLOW["tree", "root_mode"] = 1
    ALLOW["tree", "scan_digest"] = 1
    ALLOW["tree", "staged_digest"] = 1
    ALLOW["doctor", "check_counts"] = 1
    ALLOW["doctor", "report_schema_version"] = 1
    ALLOW["doctor", "report_status"] = 1
    ALLOW["doctor", "required_checks"] = 1
    ALLOW["reqchecks", "connection"] = 1
    ALLOW["reqchecks", "jsonb_integrity"] = 1
    ALLOW["reqchecks", "pgvector"] = 1
    ALLOW["reqchecks", "schema_version"] = 1
    ALLOW["checkcounts", "fail"] = 1
    ALLOW["checkcounts", "ok"] = 1
    ALLOW["checkcounts", "warn"] = 1
    ALLOW["convergence", "attempts"] = 1
    ALLOW["convergence", "max_attempts"] = 1
    ALLOW["convergence", "source_scan_a_digest"] = 1
    ALLOW["convergence", "source_scan_b_digest"] = 1
    ALLOW["exporter", "python"] = 1
    ALLOW["exporter", "version"] = 1
    # Unknown keys are rejected, so required == allowed exactly.
    REQ["top"] = "convergence,created_at_utc,doctor,exporter,generation_id,phase,remote,schema_version,sources,trees"
    REQ["remote"] = "note,uploaded"
    REQ["sources"] = "gbrain_state_dir,vault_dir"
    REQ["trees"] = ".gbrain,vault"
    REQ["tree"] = "bytes,dirs,entries,entries_digest,entries_file,files,root_mode,scan_digest,staged_digest"
    REQ["doctor"] = "check_counts,report_schema_version,report_status,required_checks"
    REQ["reqchecks"] = "connection,jsonb_integrity,pgvector,schema_version"
    REQ["checkcounts"] = "fail,ok,warn"
    REQ["convergence"] = "attempts,max_attempts,source_scan_a_digest,source_scan_b_digest"
    REQ["exporter"] = "python,version"
}

# ---------------------------------------------------------------------------
# Type-check helpers (the value under last_type/last_str/last_num)
# ---------------------------------------------------------------------------

function need_object(key) {
    if (last_type != "object") die("'" key "' must be an object")
}

function need_string(key) {
    if (last_type != "string") die("'" key "' must be a string")
}

function need_number(key) {
    if (last_type != "number") die("'" key "' must be a number")
}

function need_bool(key) {
    if (last_type != "true" && last_type != "false") die("'" key "' must be a boolean")
}

function need_int_nonneg(key) {
    need_number(key)
    if (match(last_num, "^[0-9]+$") == 0) die("'" key "' must be a non-negative integer")
}

function need_int_pos(key) {
    need_number(key)
    if (match(last_num, "^[1-9][0-9]*$") == 0) die("'" key "' must be a positive integer")
}

function need_hex64(key) {
    need_string(key)
    if (length(last_str) != 64 || match(last_str, "^[0-9a-f]{64}$") == 0) {
        die("'" key "' is not a 64-hex sha256")
    }
}

# ---------------------------------------------------------------------------
# Per-key value validation (mirrors the Python validator field by field)
# ---------------------------------------------------------------------------

function validate_value(ctx, key) {
    if (ctx == "top") {
        if (key == "schema_version") {
            need_number(key)
            if (last_num != "1") die("schema_version must be 1")
            print "schema_version\t" last_num
            return
        }
        if (key == "phase") {
            need_number(key)
            if (last_num != "1") die("phase must be 1")
            return
        }
        if (key == "generation_id") {
            need_string(key)
            if (length(last_str) != 31 || match(last_str, "^[0-9]{8}T[0-9]{12}Z-[0-9a-f]{8}$") == 0) {
                die("generation_id is not a valid generation id")
            }
            print "generation_id\t" last_str
            return
        }
        if (key == "created_at_utc") {
            need_string(key)
            return
        }
        # remote/sources/trees/doctor/convergence/exporter: objects, validated
        # structurally by parse_object; assert the object type here.
        need_object(key)
        return
    }
    if (ctx == "remote") {
        if (key == "uploaded") {
            need_bool(key)
            return
        }
        need_string(key)  # note: string (Python checks type only)
        return
    }
    if (ctx == "sources") {
        need_string(key)
        if (last_str == "") die("sources." key " must be a non-empty string")
        return
    }
    if (ctx == "tree") {
        if (key == "entries" || key == "dirs" || key == "files" || key == "bytes") {
            need_int_nonneg(key)
            return
        }
        if (key == "root_mode") {
            need_string(key)
            if (match(last_str, "^0o[0-7]{3,4}$") == 0) die("root_mode is not an octal mode string")
            return
        }
        if (key == "scan_digest" || key == "staged_digest" || key == "entries_digest") {
            need_hex64(key)
            if (key == "entries_digest") print "entries_digest\t" cur_tree "\t" last_str
            return
        }
        if (key == "entries_file") {
            need_string(key)
            if (last_str != cur_tree ".entries.txt") {
                die("entries_file must be '" cur_tree ".entries.txt'")
            }
            return
        }
        return
    }
    if (ctx == "doctor") {
        # required_checks / check_counts: objects (validated structurally);
        # report_schema_version / report_status: PRESENCE ONLY — the Python
        # validator checks neither beyond the key set, so neither do we.
        if (key == "required_checks" || key == "check_counts") {
            need_object(key)
        }
        return
    }
    if (ctx == "reqchecks") {
        need_string(key)
        if (last_str != "ok") die("required check '" key "' must be 'ok'")
        return
    }
    if (ctx == "checkcounts") {
        need_int_nonneg(key)
        if (key == "fail" && last_num != "0") die("check_counts.fail must be 0")
        return
    }
    if (ctx == "convergence") {
        if (key == "attempts" || key == "max_attempts") {
            need_int_pos(key)
            return
        }
        need_hex64(key)
        return
    }
    if (ctx == "exporter") {
        need_string(key)
        if (last_str == "") die("exporter." key " must be a non-empty string")
        return
    }
}

# ---------------------------------------------------------------------------
# Object parsing with per-context key sets
# ---------------------------------------------------------------------------

function key_value_ctx(ctx, key) {
    if (ctx == "top") {
        if (key == "remote") return "remote"
        if (key == "sources") return "sources"
        if (key == "trees") return "trees"
        if (key == "doctor") return "doctor"
        if (key == "convergence") return "convergence"
        if (key == "exporter") return "exporter"
        return "scalar"
    }
    if (ctx == "doctor") {
        if (key == "required_checks") return "reqchecks"
        if (key == "check_counts") return "checkcounts"
        return "scalar"
    }
    if (ctx == "trees") {
        cur_tree = key
        return "tree"
    }
    return "scalar"
}

function check_required(ctx, seen,   n, i, reqs) {
    if (ctx == "trees") {
        if (index(seen, ",.gbrain,") == 0) die("trees is missing required key '.gbrain'")
        if (index(seen, ",vault,") == 0) die("trees is missing required key 'vault'")
        return
    }
    n = split(REQ[ctx], reqs, ",")
    for (i = 1; i <= n; i = i + 1) {
        if (index(seen, "," reqs[i] ",") == 0) {
            die("'" ctx "' block is missing required key '" reqs[i] "'")
        }
    }
}

function parse_object(ctx,   seen, key, c) {
    pos = pos + 1  # consume '{'
    skip_ws()
    seen = ""
    if (pos <= len && peek() == "}") {
        pos = pos + 1
        check_required(ctx, seen)
        return
    }
    for (;;) {
        skip_ws()
        if (pos > len) die("unterminated object")
        if (peek() != "\"") die("object key is not a string")
        key = parse_string()
        if (index(seen, "," key ",") > 0) die("duplicate key '" key "'")
        seen = seen "," key ","
        if (ctx == "trees") {
            if (key != ".gbrain" && key != "vault") {
                die("unknown tree '" key "' (manifest trees must contain exactly '.gbrain' and 'vault')")
            }
        } else if (!(ALLOW[ctx, key])) {
            die("unknown key '" key "' in '" ctx "' block")
        }
        skip_ws()
        if (pos > len || peek() != ":") die("expected ':' after object key")
        pos = pos + 1
        parse_value(key_value_ctx(ctx, key))
        validate_value(ctx, key)
        skip_ws()
        if (pos > len) die("unterminated object")
        c = peek()
        if (c == ",") {
            pos = pos + 1
            continue
        }
        if (c == "}") {
            pos = pos + 1
            check_required(ctx, seen)
            return
        }
        die("expected ',' or '}' in object")
    }
}

function parse_array(   c) {
    die("array values are not part of the manifest schema")
}

function parse_value(vctx,   c) {
    skip_ws()
    if (pos > len) die("unexpected end of input")
    c = peek()
    if (c == "{") {
        parse_object(vctx)
        # Set AFTER the nested parse: the last nested value clobbers
        # last_type while parse_object walks the sub-object.
        last_type = "object"
        return
    }
    if (c == "[") {
        last_type = "array"
        parse_array()
        return
    }
    if (c == "\"") {
        last_str = parse_string()
        last_type = "string"
        return
    }
    if (c == "t") {
        parse_literal("true")
        last_type = "true"
        return
    }
    if (c == "f") {
        parse_literal("false")
        last_type = "false"
        return
    }
    if (c == "n") {
        parse_literal("null")
        last_type = "null"
        return
    }
    if (c == "-" || (c >= "0" && c <= "9")) {
        parse_number()
        last_type = "number"
        return
    }
    die("unexpected character")
}

{
    buf = buf $0 "\n"
}

END {
    len = length(buf)
    pos = 1
    skip_ws()
    if (pos > len) die("empty input")
    parse_value("top")
    if (last_type != "object") die("top-level value must be a JSON object")
    skip_ws()
    if (pos <= len) die("trailing content after the JSON value")
    exit 0
}
