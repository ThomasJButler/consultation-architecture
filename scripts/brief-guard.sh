#!/bin/sh
# Refuses a commit that carries a filename or marker from the received brief.
#
# Only generic patterns live here, on purpose: the specific strings that must
# never be committed can't sit in a tracked file. Those are checked by a second
# script outside the repo, wired in from .git/hooks/pre-commit (see SECURITY.md).
#
#   sh scripts/brief-guard.sh        check what's staged
#   sh scripts/brief-guard.sh --all  check every tracked file (CI)
#
# .gitignore and this script are skipped for content: both have to name the
# markers to do their job.
set -u
patterns='greenhouse\.io
grnh\.se
Take_Home_Task
taskemail
example-consul
MSIP_Label'
skip='^(\.gitignore|scripts/brief-guard\.sh)$'
if [ "${1:-}" = "--all" ]; then
  names=$(git ls-files)
  files=$(printf '%s\n' "$names" | grep -vE "$skip" || true)
  content=""
  [ -n "$files" ] && content=$(printf '%s\n' "$files" | tr '\n' '\0' | xargs -0 cat)
else
  names=$(git diff --cached --name-only --diff-filter=ACMR)
  files=$(printf '%s\n' "$names" | grep -vE "$skip" || true)
  content=""
  [ -n "$files" ] && content=$(printf '%s\n' "$files" | xargs git diff --cached -U0 --diff-filter=ACMR -- | grep '^+' | grep -v '^+++' || true)
fi
status=0
for pat in $patterns; do
  if printf '%s\n' "$names" | grep -qiE -- "$pat"; then echo "brief-guard: a filename matches '$pat'"; status=1; fi
  if printf '%s\n' "$content" | grep -qiE -- "$pat"; then echo "brief-guard: content matches '$pat'"; status=1; fi
done
[ "$status" -eq 0 ] || echo "brief-guard: commit blocked"
exit $status
