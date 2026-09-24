#!/bin/sh
# This snap uses classic confinement, so `snap run` does *not* prepend
# $SNAP/usr/bin or $SNAP/bin to PATH the way it would for a strictly
# confined snap — it deliberately leaves the host's PATH/PYTHONPATH
# alone. That means the bundled interpreter at $SNAP/bin/python3 won't
# find its own stdlib or the app's site-packages unless we point it
# there explicitly. Without this, `env python3` in the console-script
# shebang resolves to whatever python3 happens to be on the host's
# PATH (which may be a different, ABI-incompatible version), and even
# the bundled interpreter can't locate its own stdlib/site-packages on
# its own.
set -e
export PYTHONHOME="$SNAP/usr"
export PYTHONPATH="$SNAP/lib/python3.12/site-packages${PYTHONPATH:+:$PYTHONPATH}"
# snapd resolves only the *first* word of an app's `command:` to an
# absolute $SNAP path; any further words (e.g. a script path passed as
# an argument) are left as literal relative args, which resolve against
# whatever cwd the process starts in — not $SNAP. So `command:` here is
# a single relative script path (e.g. `bin/automated-ken-runner run`),
# already resolved to absolute by snapd and passed to us as "$@"; we
# just need to run it with the bundled interpreter explicitly.
exec "$SNAP/bin/python3" "$@"
