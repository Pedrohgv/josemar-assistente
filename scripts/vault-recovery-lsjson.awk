#!/usr/bin/awk -f
# vault-recovery-lsjson.awk - strict rclone `lsjson` inventory parser
# (POSIX awk; the pinned rclone image has busybox awk only — no python3,
# no jq).
#
# Replaces the previous human-readable `rclone lsd` column parsing: the
# remote generation inventory (committed namespace listing in the uploader's
# retention pass and the recover step's `list-remote`) is now read as
# MACHINE JSON from `rclone lsjson <dir> --dirs-only` and parsed here with
# full RFC 8259 well-formedness validation (the same grammar as
# vault-recovery-json.awk) plus strict structural checks:
#   - the top-level value MUST be an array,
#   - every array entry MUST be an object carrying a string `Name` and a
#     boolean `IsDir` (duplicate keys rejected),
#   - unknown extra fields (rclone adds backend-specific keys such as `ID`,
#     `OrigID`, `Tier`) are accepted — the lsjson shape varies per backend,
#     but the fields the scripts rely on must be present and well-typed,
#   - trailing content after the array is rejected.
#
# Output: one line per entry, TAB-separated, in array order:
#   <Name>\t<IsDir-as-0/1>
# The callers reject any entry whose IsDir is not 1 (a `--dirs-only`
# listing must contain directories only) and validate every Name as a
# strict generation id.
#
# Exit: 0 with the entries printed, 1 on any violation (diagnostic on
# stderr). POSIX-awk safe (substr/length/index only, locals via the
# parameter list).

function die(msg) {
    print "vault-recovery-lsjson: invalid lsjson: " msg > "/dev/stderr"
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
    # Returns the RAW text between the quotes (no unescaping; the only
    # values compared are plain ASCII generation ids, so raw comparison is
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
# Entry-object parsing: capture Name (string) and IsDir (boolean); accept
# any other key with any JSON value (backend-specific fields).
# ---------------------------------------------------------------------------

function parse_entry_object(   name, isdir, have_name, have_isdir, key, c) {
    pos = pos + 1  # consume '{'
    skip_ws()
    if (pos <= len && peek() == "}") {
        pos = pos + 1
        die("entry object is missing Name/IsDir")
    }
    for (;;) {
        skip_ws()
        if (pos > len) die("unterminated entry object")
        if (peek() != "\"") die("entry key is not a string")
        key = parse_string()
        if (key == "Name" && have_name) die("duplicate Name key")
        if (key == "IsDir" && have_isdir) die("duplicate IsDir key")
        skip_ws()
        if (pos > len || peek() != ":") die("expected ':' after entry key")
        pos = pos + 1
        skip_ws()
        if (key == "Name") {
            if (peek() != "\"") die("entry Name must be a string")
            name = parse_string()
            have_name = 1
        } else if (key == "IsDir") {
            if (substr(buf, pos, 4) == "true") {
                isdir = 1
                pos = pos + 4
            } else if (substr(buf, pos, 5) == "false") {
                isdir = 0
                pos = pos + 5
            } else {
                die("entry IsDir must be a boolean")
            }
            have_isdir = 1
        } else {
            parse_value(3)  # unknown backend field: any JSON value
        }
        skip_ws()
        if (pos > len) die("unterminated entry object")
        c = peek()
        if (c == ",") {
            pos = pos + 1
            continue
        }
        if (c == "}") {
            pos = pos + 1
            break
        }
        die("expected ',' or '}' in entry object")
    }
    if (!have_name || !have_isdir) die("entry object is missing Name/IsDir")
    n_entries = n_entries + 1
    entry_name[n_entries] = name
    entry_isdir[n_entries] = isdir
}

# ---------------------------------------------------------------------------
# Generic value parsing (full grammar; used for unknown entry fields)
# ---------------------------------------------------------------------------

function parse_array_body(is_top,   c) {
    # is_top=1: the TOP-LEVEL array of the lsjson output — every element
    # must be an entry object. is_top=0: a nested array inside an unknown
    # backend field value — any JSON value is accepted.
    skip_ws()
    if (pos <= len && peek() == "]") {
        pos = pos + 1
        return
    }
    for (;;) {
        skip_ws()
        if (pos > len) die("unterminated array")
        if (is_top) {
            if (peek() != "{") die("array entries must be objects")
            parse_entry_object()
        } else {
            parse_value(2)
        }
        skip_ws()
        if (pos > len) die("unterminated array")
        c = peek()
        if (c == ",") {
            pos = pos + 1
            continue
        }
        if (c == "]") {
            pos = pos + 1
            return
        }
        die("expected ',' or ']' in array")
    }
}

function parse_value(depth,   c) {
    skip_ws()
    if (pos > len) die("unexpected end of input")
    c = peek()
    if (c == "{") {
        pos = pos + 1
        parse_object_body(depth)
        return
    }
    if (c == "[") {
        pos = pos + 1
        parse_array_body(0)
        return
    }
    if (c == "\"") {
        parse_string()
        return
    }
    if (c == "t") {
        parse_literal("true")
        return
    }
    if (c == "f") {
        parse_literal("false")
        return
    }
    if (c == "n") {
        parse_literal("null")
        return
    }
    if (c == "-" || (c >= "0" && c <= "9")) {
        parse_number()
        return
    }
    die("unexpected character")
}

function parse_object_body(depth,   c) {
    # Non-entry object (e.g. a backend field value): consume the whole
    # object with the full grammar, without capturing anything.
    skip_ws()
    if (pos <= len && peek() == "}") {
        pos = pos + 1
        return
    }
    for (;;) {
        skip_ws()
        if (pos > len) die("unterminated object")
        if (peek() != "\"") die("object key is not a string")
        parse_string()
        skip_ws()
        if (pos > len || peek() != ":") die("expected ':' after object key")
        pos = pos + 1
        parse_value(depth + 1)
        skip_ws()
        if (pos > len) die("unterminated object")
        c = peek()
        if (c == ",") {
            pos = pos + 1
            continue
        }
        if (c == "}") {
            pos = pos + 1
            return
        }
        die("expected ',' or '}' in object")
    }
}

{
    buf = buf $0 "\n"
}

END {
    len = length(buf)
    pos = 1
    skip_ws()
    if (pos > len) die("empty input")
    if (peek() != "[") die("top-level value must be an array")
    pos = pos + 1
    parse_array_body(1)
    skip_ws()
    if (pos <= len) die("trailing content after the JSON array")
    for (i = 1; i <= n_entries; i = i + 1) {
        printf "%s\t%d\n", entry_name[i], entry_isdir[i]
    }
    exit 0
}
