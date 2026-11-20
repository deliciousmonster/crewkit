#!/bin/sh
# Push this repository as deliciousmonster, whatever gh account is active. The token goes into the
# environment of the push alone and is never printed.
set -eu
ACCOUNT=deliciousmonster
REPO_DIR=$(cd "$(dirname "$0")/.." && pwd)

GH_TOKEN=$(gh auth token --user "$ACCOUNT") || { echo "push.sh: gh has no login for $ACCOUNT" >&2; exit 2; }
export GH_TOKEN

who=$(gh api user --jq .login)
[ "$who" = "$ACCOUNT" ] || { echo "push.sh: token acts as $who, not $ACCOUNT" >&2; exit 2; }

url=$(git -C "$REPO_DIR" remote get-url origin)
[ "$url" = "https://github.com/$ACCOUNT/crewkit.git" ] || { echo "push.sh: unexpected origin $url" >&2; exit 2; }
# A push goes to pushurl when one is set, so it must name the same repository.
for push_url in $(git -C "$REPO_DIR" config --get-all remote.origin.pushurl || true); do
    [ "$push_url" = "https://github.com/$ACCOUNT/crewkit.git" ] || { echo "push.sh: unexpected origin pushurl $push_url" >&2; exit 2; }
done
# Private or public both push; anything else (internal, or no answer) is a visibility nobody chose.
visibility=$(gh repo view "$ACCOUNT/crewkit" --json visibility --jq .visibility) || visibility=unknown
case "$visibility" in
    PRIVATE|PUBLIC) ;;
    *) echo "push.sh: repository visibility is $visibility, not PRIVATE or PUBLIC" >&2; exit 2 ;;
esac

git -C "$REPO_DIR" push origin "$@"
