#!/bin/sh
# Drop to an unprivileged user before starting Prudify.
#
# Two reasons this exists rather than a bare `USER` line in the Dockerfile:
#
#   1. Files written into the clean library must be owned by the same user as
#      the rest of the media, or Plex and Audiobookshelf can read the cleaned
#      books but cannot rename, retag or delete them. PUID/PGID is the
#      convention every self-hosted media container follows for this.
#   2. ffmpeg and the Whisper backend parse untrusted media. A memory-safety
#      bug in a demuxer, handed a malicious file, should not be a root
#      compromise of the container.
set -e

PUID="${PUID:-1000}"
PGID="${PGID:-1000}"
UMASK="${UMASK:-022}"

if [ "$(id -u)" = "0" ]; then
    groupmod -o -g "$PGID" prudify 2>/dev/null || true
    usermod  -o -u "$PUID" -g "$PGID" prudify 2>/dev/null || true

    # Only the paths we own. Never touch the media mounts: /audiobooks is
    # read-only, and a recursive chown of a multi-terabyte library would add
    # hours to every container start.
    chown -R prudify:prudify /config 2>/dev/null || true
    [ -d /work ] && chown -R prudify:prudify /work 2>/dev/null || true

    umask "$UMASK"
    exec gosu prudify "$@"
fi

# Already unprivileged (e.g. `docker run --user`); just run.
umask "$UMASK"
exec "$@"
