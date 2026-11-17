#!/usr/bin/env bash
# Regression battery for the AI co-author guard. Takes the hook path as $1 so
# the same suite validates every copy in the fleet during propagation.
#
#   .githooks/commit-msg.test.sh [path-to-hook]
#
# Each case is a commit message plus the expected verdict. Every reject case is
# a bypass that was live at some point; the accept cases are false positives that
# were live. None is hypothetical.
set -uo pipefail
HOOK="${1:-$(cd "$(dirname "$0")" && pwd)/commit-msg}"
# Absolute, because we run the hook from a neutral CWD below.
case "$HOOK" in /*) ;; *) HOOK="$(cd "$(dirname "$HOOK")" 2>/dev/null && pwd)/$(basename "$HOOK")" ;; esac
# Without this a mistyped path scores a plausible-looking 24/10 (every reject
# "passes" because a missing hook exits non-zero) instead of erroring.
[ -f "$HOOK" ] || { printf 'no such hook: %s\n' "$HOOK" >&2; exit 2; }
# Git runs a hook by executing it directly (its shebang + exec bit), never as
# `bash <hook>`. A propagated copy that lost its exec bit is silently skipped by
# git -- the guard is simply off -- yet it would still pass every case below if we
# ran it under `bash`. So refuse a non-executable candidate here, and invoke the
# hook directly (not via `bash`) in every case, to cover the installation
# contract as well as the parsing.
[ -x "$HOOK" ] || { printf 'hook is not executable: %s\n' "$HOOK" >&2; exit 2; }

TMP=$(mktemp -d) || exit 2
unset GIT_DIR GIT_WORK_TREE GIT_INDEX_FILE GIT_COMMON_DIR
trap 'rm -rf "$TMP"' EXIT

# Run from a scratch repo, NOT the caller's. A hook copy that still carries the
# old repo-relative ".githooks/commit-msg" delegation candidate would otherwise
# exec the CALLER's hook and be scored on its behaviour — which made a sweep of
# known-vulnerable copies report a clean 25/25. Isolation is the whole point.
git init -q "$TMP/repo" || { echo "cannot create scratch repo" >&2; exit 2; }
cd "$TMP/repo" || exit 2
pass=0; fail=0

# The message is an ARGUMENT, not stdin. Piping into this function would run it
# in a subshell under bash, and the pass/fail counters would silently never
# leave it — the suite then reports 0 passed, 0 failed and exits 0.
# Pin the environment. Inheriting LC_ALL/GREP_OPTIONS made the suite score the
# caller's shell rather than the hook: the invalid-UTF-8 case only discriminates
# under a UTF-8 locale, and a copy missing `GREP_OPTIONS=` still scored 25/25.
# TEST_LOCALE lets a runner without en_US.UTF-8 pass C.UTF-8 instead.
# $4 is optional extra env for cases that ARE about the environment.
t() {
  local exp="$1" name="$2" msg="$3" extra="${4:-}" got
  if [ -z "$msg" ]; then rm -rf "$TMP/m"; mkdir -p "$TMP/m"
  else rm -rf "$TMP/m"; printf '%b' "$msg" > "$TMP/m"; fi
  if env -u AISIG_ALLOW -u AISIG_DELEGATED -u GREP_OPTIONS \
         LC_ALL="${TEST_LOCALE:-en_US.UTF-8}" $extra \
         "$HOOK" "$TMP/m" >/dev/null 2>&1
  then got=accept; else got=reject; fi
  if [ "$got" = "$exp" ]; then
    pass=$((pass + 1))
  else
    fail=$((fail + 1)); printf 'FAIL %-42s want=%s got=%s\n' "$name" "$exp" "$got"
  fi
}

# --- must REJECT -----------------------------------------------------------
t reject "canonical" "f: x\n\nCo-authored-by: Claude <noreply@anthropic.com>\n"
t reject "titlecase" "f: x\n\nCo-Authored-By: Claude Opus 5 <noreply@anthropic.com>\n"
t reject "lowercase" "f: x\n\nco-authored-by: claude <x@y.z>\n"
t reject "leading tab" "f: x\n\n\tCo-authored-by: Claude <x@y.z>\n"
t reject "space before colon" "f: x\n\nCo-authored-by : Claude <x@y.z>\n"
t reject "tab before colon" "f: x\n\nCo-authored-by\t:\tClaude <x@y.z>\n"
t reject "folded value" "f: x\n\nCo-authored-by:\n  Claude <noreply@anthropic.com>\n"
t reject "folded email" "f: x\n\nCo-authored-by: A Person\n  <noreply@anthropic.com>\n"
t reject "indented + folded" "f: x\n\n  Co-authored-by: Somebody\n    <noreply@anthropic.com>\n"
t reject "no trailing newline" "f: x\n\nCo-authored-by: Someone <noreply@anthropic.com>"
t reject "CRLF" "f: x\r\n\r\nCo-authored-by: Claude <x@y.z>\r\n"
t reject "GitHub Copilot" "f: x\n\nCo-authored-by: GitHub Copilot <copilot@github.com>\n"
t reject "copilot bot" "f: x\n\nCo-authored-by: github-copilot[bot] <bot@github.com>\n"
t reject "cursor" "f: x\n\nCo-authored-by: Cursor <cursoragent@cursor.com>\n"
t reject "second of two" "f: x\n\nCo-authored-by: J <j@ex.com>\nCo-authored-by: C <c@anthropic.com>\n"
t reject "jules bot" "f: x\n\nCo-authored-by: Jules <google-labs-jules[bot]@users.noreply.github.com>\n"
t reject "openhands" "f: x\n\nCo-authored-by: OpenHands <openhands@all-hands.dev>\n"
t reject "invalid UTF-8 in name" "f: x\n\nCo-authored-by: Jos\xe9 C <noreply@anthropic.com>\n"
t reject "NUL byte in line" "f: x\n\nCo-authored-by: N\x00ul <noreply@anthropic.com>\n"
# git strips `#` lines AFTER the hook accepts the message, which folds the
# continuation into the trailer: this was accepted and committed as an AI trailer.
t reject "comment line between trailer and fold" "f: x\n\nCo-authored-by: Foo\n# c\n  <noreply@anthropic.com>\n"
# `\b` does not fire next to `_` or a digit, so these used to be accepted.
t reject "underscore-joined name"   "f: x\n\nCo-authored-by: Anthropic_Bot <a@b.c>\n"
t reject "underscore-joined email"  "f: x\n\nCo-authored-by: x <claude_bot@x.c>\n"
t reject "digit-suffixed name"      "f: x\n\nCo-authored-by: Claude4 <a@b.c>\n"
# Concatenated bot names: a letter touching the token must not hide it.
t reject "ClaudeBot"                "f: x\n\nCo-authored-by: ClaudeBot <a@b.c>\n"
t reject "claudecode"               "f: x\n\nCo-authored-by: claudecode <a@b.c>\n"
t reject "OpenAIBot"                "f: x\n\nCo-authored-by: OpenAIBot <a@b.c>\n"
t reject "CursorAgent"              "f: x\n\nCo-authored-by: CursorAgent <a@b.c>\n"
t reject "cursoragent noreply login" "f: x\n\nCo-authored-by: Bot <1+cursoragent@users.noreply.github.com>\n"
t reject "Anthropics"               "f: x\n\nCo-authored-by: Anthropics <a@b.c>\n"

# --- must ACCEPT -----------------------------------------------------------
t accept "clean message" "f: x\n\nplain body\n"
t accept "human @example.com" "f: x\n\nCo-authored-by: Jane Dev <jane@example.com>\n"
t accept "champ (amp substring)" "f: x\n\nCo-authored-by: Sam Champ <champ@example.com>\n"
t accept "human named Jules" "f: x\n\nCo-authored-by: Jules Verne <jules@verne.fr>\n"
t accept "comment line, no AI in trailer" "f: x\n\nCo-authored-by: Jane Dev <jane@example.com>\n# note\n"
t accept "short token in subject, human trailer" "f: bump amp dep\n\nCo-authored-by: Jane Dev <jane@example.com>\n"
t accept "short token in body, human trailer"    "f: x\n\nCo-authored-by: Jane Dev <jane@example.com>\n\nuse the cursor\n"
t accept "precursor (cursor substring)" "f: x\n\nCo-authored-by: Pat Precursor <p@example.com>\n"
t accept "Hubbard (bard substring)"   "f: x\n\nCo-authored-by: Ann Hubbard <a@example.com>\n"
t accept "human named Cline" "f: x\n\nCo-authored-by: Patsy Cline <patsy@cline.org>\n"
t accept "prose naming claude" "f: x\n\nswitch the model pin to anthropic/claude-sonnet-5\n"
# Pins LC_ALL=C on the unfold awk. git does not fold a line led by non-ASCII
# whitespace, so this is not a trailer and nothing would be credited; without
# the C locale awk folds it anyway and the guard over-rejects a valid message.
t accept "NBSP-led continuation"              "f: x\n\nCo-authored-by: P\n\xc2\xa0 <noreply@anthropic.com>\n"

# --- environment-shaped cases ----------------------------------------------
t reject "GREP_OPTIONS=-x cannot disable it" "f: x\n\nCo-authored-by: Claude <noreply@anthropic.com>\n" "GREP_OPTIONS=-x"
t reject "GREP_OPTIONS=-m0 cannot disable it" "f: x\n\nCo-authored-by: Claude <noreply@anthropic.com>\n" "GREP_OPTIONS=-m0"
t reject "directory as message file"          ""

# --- delegation ------------------------------------------------------------
# A delegated .git/hooks/commit-msg may REWRITE the message. The guard re-scans
# afterwards; that re-scan must unfold, or a delegate appending a folded trailer
# lands credited — a message the guard rejects when a human writes it.
d() {
  local exp="$1" name="$2" append="$3" got
  rm -rf "$TMP/m"   # the directory case leaves $TMP/m as a dir; the delegate
                    # would then fail to append and every case here would
                    # "reject" for the wrong reason.
  mkdir -p "$TMP/repo/.git/hooks"
  { printf '#!/bin/sh\n'; printf 'printf %s "$1"\n' "'$append' >>"; printf 'exit 0\n'; } \
    > "$TMP/repo/.git/hooks/commit-msg"
  chmod +x "$TMP/repo/.git/hooks/commit-msg"
  printf 'f: clean, no trailer\n' > "$TMP/m"
  if env -u AISIG_ALLOW -u AISIG_DELEGATED -u GREP_OPTIONS \
         LC_ALL="${TEST_LOCALE:-en_US.UTF-8}" "$HOOK" "$TMP/m" >/dev/null 2>&1
  then got=accept; else got=reject; fi
  rm -f "$TMP/repo/.git/hooks/commit-msg"
  if [ "$got" = "$exp" ]; then
    pass=$((pass + 1))
  else
    fail=$((fail + 1)); printf 'FAIL %-42s want=%s got=%s\n' "$name" "$exp" "$got"
  fi
}
d reject "delegate appends plain trailer"  '\nCo-authored-by: Claude <noreply@anthropic.com>\n'
d reject "delegate appends folded trailer" '\nCo-authored-by: A Person\n  <noreply@anthropic.com>\n'
d accept "delegate appends human trailer"  '\nCo-authored-by: Jane Dev <jane@example.com>\n'

# --- output-shaped cases ---------------------------------------------------
# The suite otherwise discards the hook's output, so two hardenings were
# invisible to it: the control-character sanitiser and the bypass announcement.
# Both are defences you only notice when they are gone, which is exactly the
# kind that rots silently. Assert on stderr.
o() {
  local name="$1" msg="$2" want="$3" extra="${4:-}" out
  rm -rf "$TMP/m"; printf '%b' "$msg" > "$TMP/m"
  out=$(env -u AISIG_ALLOW -u AISIG_DELEGATED -u GREP_OPTIONS \
        LC_ALL="${TEST_LOCALE:-en_US.UTF-8}" $extra \
        "$HOOK" "$TMP/m" 2>&1 >/dev/null || true)
  if printf '%s' "$out" | LC_ALL=C grep -qa "$want"; then
    pass=$((pass + 1))
  else
    fail=$((fail + 1)); printf 'FAIL %-42s stderr lacked /%s/\n' "$name" "$want"
  fi
}
# A bypass must announce itself; exported from a profile or CI it would
# otherwise disable the guard permanently and invisibly.
o "AISIG_ALLOW announces itself" "f: x\n\nCo-authored-by: Claude <noreply@anthropic.com>\n" \
  "AISIG_ALLOW=1" "AISIG_ALLOW=1"
# ESC and CR in a fetched/squashed message must not reach the terminal raw:
# either can repaint the rejection as a success.
o "ESC sanitised in rejection"    "f: x\n\nCo-authored-by: \033[2JClaude <noreply@anthropic.com>\n" "?\[2J"
o "CR sanitised in rejection"     "f: x\n\nCo-authored-by: Claude <noreply@anthropic.com>\rSPOOF\n" "?SPOOF"

# Inverse of o(): the rejection must NOT contain the bytes. [[:cntrl:]] let UTF-8
# C1 controls (U+009B is CSI to xterm/VTE) and bidi overrides (U+202E) through.
on() {
  local name="$1" msg="$2" bad="$3" out
  rm -rf "$TMP/m"; printf '%b' "$msg" > "$TMP/m"
  out=$(env -u AISIG_ALLOW -u AISIG_DELEGATED -u GREP_OPTIONS \
        LC_ALL="${TEST_LOCALE:-en_US.UTF-8}" "$HOOK" "$TMP/m" 2>&1 >/dev/null || true)
  if ! printf '%s' "$out" | LC_ALL=C grep -qa 'AI co-author trailer rejected'; then
    fail=$((fail + 1)); printf 'FAIL %-42s message was not rejected\n' "$name"
  elif printf '%s' "$out" | LC_ALL=C grep -qa "$(printf '%b' "$bad")"; then
    fail=$((fail + 1)); printf 'FAIL %-42s stderr still held the raw bytes\n' "$name"
  else
    pass=$((pass + 1))
  fi
}
on "C1 CSI (U+009B) sanitised"   "f: x\n\nCo-authored-by: \xc2\x9b2JClaude <noreply@anthropic.com>\n" '\xc2\x9b'
on "bidi RLO (U+202E) sanitised" "f: x\n\nCo-authored-by: \xe2\x80\xaeClaude <noreply@anthropic.com>\n" '\xe2\x80\xae'

# --- arbitrary-code-execution regression ---------------------------------------
# The old hook delegated to a RELATIVE .githooks/commit-msg. Git runs hooks from
# the worktree root and keeps the exec bit on clone, so committing in a cloned
# repo ran that repo's script. Plant one and prove it never runs.
rm -rf "$TMP/sentinel" "$TMP/repo/.githooks"
mkdir -p "$TMP/repo/.githooks"
printf '#!/bin/sh\ntouch "%s"\nexit 0\n' "$TMP/sentinel" > "$TMP/repo/.githooks/commit-msg"
chmod +x "$TMP/repo/.githooks/commit-msg"
printf 'f: clean\n' > "$TMP/m"
env -u AISIG_ALLOW -u AISIG_DELEGATED -u GREP_OPTIONS \
    LC_ALL="${TEST_LOCALE:-en_US.UTF-8}" "$HOOK" "$TMP/m" >/dev/null 2>&1 || true
if [ -e "$TMP/sentinel" ]; then
  fail=$((fail + 1)); printf 'FAIL %-42s a repo-relative hook was executed\n' "no exec of working-tree .githooks/commit-msg"
else
  pass=$((pass + 1))
fi
rm -rf "$TMP/repo/.githooks" "$TMP/sentinel"

printf '%s: %d passed, %d failed\n' "$(basename "$HOOK")" "$pass" "$fail"
[ "$fail" -eq 0 ]
