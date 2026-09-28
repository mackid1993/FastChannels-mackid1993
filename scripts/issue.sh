#!/usr/bin/env bash
# Status issues for CI, matched by exact title (the search index lags, so it isn't used).
#
#   scripts/issue.sh open   "<title>" <body-file>  comment on the open issue with this title, or create it
#   scripts/issue.sh close  "<title>" "<comment>"  close every open issue with this title
#   scripts/issue.sh exists "<title>"              exit 0 if an open issue with this title exists
set -euo pipefail

action=${1:?action}
title=${2:?title}
repo=${GITHUB_REPOSITORY:?}

open_issues() {
    gh issue list -R "$repo" --state open --limit 100 --json number,title \
        --jq ".[] | select(.title == \"$title\") | .number"
}

case "$action" in
    open)
        n=$(open_issues | head -1)
        if [ -n "$n" ]; then
            gh issue comment "$n" -R "$repo" --body-file "${3:?body file}"
        else
            gh issue create -R "$repo" --title "$title" --body-file "${3:?body file}"
        fi
        ;;
    close)
        for n in $(open_issues); do
            gh issue close "$n" -R "$repo" --comment "${3:?comment}"
        done
        ;;
    exists)
        [ -n "$(open_issues)" ]
        ;;
    *)
        echo "unknown action: $action" >&2
        exit 2
        ;;
esac
