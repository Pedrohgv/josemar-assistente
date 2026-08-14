#!/usr/bin/awk -f
# vault-recovery-json.awk - strict JSON well-formedness validator (POSIX awk).
#
# Used by the Phase-2 shell steps (vault-recovery-uploader.sh and
# vault-recovery-recover.sh), which run in the pinned rclone image (Alpine +
# busybox only: no python3, no jq). It validates that the input is EXACTLY
# one well-formed JSON value (full RFC 8259 grammar: objects, arrays,
# strings with escapes incl. \uXXXX, numbers, true/false/null, no trailing
# content). The shell steps previously accepted a manifest when its required
# fields were grep-visible even if the document was not valid JSON at all
# (council fix: strict JSON schema validation); this validator closes that
# gap BEFORE any field is extracted.
#
# Exit status: 0 when the input is a single well-formed JSON value,
# 1 otherwise (diagnostic on stderr).
#
# POSIX-awk safe: only substr/length/index/peek-style operations, explicit
# local variables via the parameter list, recursion depth bounded by the
# manifest's shallow nesting.

function die(msg) {
    print "vault-recovery-json: invalid JSON: " msg > "/dev/stderr"
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

function parse_string() {
    # Called with buf[pos] == '"'. Consumes the full string literal.
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
            return
        } else if (c < " ") {
            die("control character in string")
        } else {
            pos = pos + 1
        }
    }
    die("unterminated string")
}

function parse_number() {
    # Called with buf[pos] == '-' or a digit.
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

function parse_value() {
    skip_ws()
    if (pos > len) die("unexpected end of input")
    c = peek()
    if (c == "{") {
        pos = pos + 1
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
            parse_value()
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
    if (c == "[") {
        pos = pos + 1
        skip_ws()
        if (pos <= len && peek() == "]") {
            pos = pos + 1
            return
        }
        for (;;) {
            parse_value()
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

{
    buf = buf $0 "\n"
}

END {
    len = length(buf)
    pos = 1
    parse_value()
    skip_ws()
    if (pos <= len) die("trailing content after the JSON value")
    exit 0
}
