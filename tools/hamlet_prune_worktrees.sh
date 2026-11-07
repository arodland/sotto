#!/bin/sh
# List, and with --yes remove, the extra git worktrees of a sotto checkout that are safe to drop:
# no uncommitted or untracked files, and a HEAD that some origin/* branch already contains, so
# every commit in it is on GitHub. Worktrees under /tmp (old Claude session scratchpads) are
# left alone while a process runs from them; `git worktree prune` forgets them once they're gone.
#
#   sh tools/hamlet_prune_worktrees.sh          what it would remove, and why it keeps the rest
#   sh tools/hamlet_prune_worktrees.sh --yes    remove them
set -eu
YES=${1:-}
R=$(git rev-parse --show-toplevel)
git -C "$R" fetch -q origin
git -C "$R" worktree prune
git -C "$R" worktree list --porcelain | sed -n 's/^worktree //p' | while read -r W; do
  [ "$W" = "$R" ] && continue
  case $W in
    /tmp/*)
      if [ -n "$(ps -eo args | grep -F "$(dirname "$W")" | grep -v grep || true)" ]; then
        echo "keep   $W  (a process runs from its scratchpad)"; continue
      fi ;;
  esac
  if [ -n "$(git -C "$W" status --porcelain 2>/dev/null)" ]; then
    echo "keep   $W  (uncommitted or untracked files)"; continue
  fi
  H=$(git -C "$W" rev-parse HEAD)
  if [ -z "$(git -C "$R" branch -r --contains "$H" 2>/dev/null)" ]; then
    echo "keep   $W  ($H is on no origin branch)"; continue
  fi
  if [ "$YES" = --yes ]; then
    git -C "$R" worktree remove "$W" && echo "removed $W"
  else
    echo "remove $W  ($(git -C "$W" log -1 --format='%h %s' | cut -c1-60))"
  fi
done
[ "$YES" = --yes ] || echo "(nothing removed: run again with --yes)"
