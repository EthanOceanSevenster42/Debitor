#!/usr/bin/env bash
#
# Install (or refresh) the systemd timers that drive the FSA Debtor System's two
# scheduled jobs: the hourly Xero sync and the hourly due-check that sends the
# lawyer progress report.
#
# Idempotent - safe to run on every deploy. Re-running rewrites the unit files
# from the repo, reloads systemd and re-enables the timers, so the schedule is
# whatever is committed here rather than whatever someone once typed into
# crontab. Nothing is sent as a side effect: send_lawyer_report self-gates on the
# in-app schedule, and stays off until a Super Admin enables it.
#
#   sudo deploy/install-timers.sh              # install / refresh, then enable
#   sudo deploy/install-timers.sh --status     # just show what is scheduled
#   sudo deploy/install-timers.sh --uninstall  # stop, disable and remove
#
# Overridable: APP_DIR, PYTHON, RUN_AS.
#
set -euo pipefail

APP_DIR="${APP_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
PYTHON="${PYTHON:-$APP_DIR/.venv/bin/python}"
# Run as whoever owns the checkout, not root - the job writes logs and media.
RUN_AS="${RUN_AS:-$(stat -c '%U' "$APP_DIR/manage.py")}"

UNIT_DIR=/etc/systemd/system
SRC="$APP_DIR/deploy/systemd"
UNITS=(fsa-sync.service fsa-sync.timer fsa-lawyer-report.service fsa-lawyer-report.timer)
TIMERS=(fsa-sync.timer fsa-lawyer-report.timer)

die() { echo "install-timers: $*" >&2; exit 1; }

command -v systemctl >/dev/null || die "systemd not found - use cron instead (see README section 9)."

case "${1:-}" in
  --status)
    systemctl list-timers --all 'fsa-*'
    exit 0
    ;;
  --uninstall)
    [ "$(id -u)" -eq 0 ] || die "must run as root."
    systemctl disable --now "${TIMERS[@]}" 2>/dev/null || true
    rm -f "${UNITS[@]/#/$UNIT_DIR/}"
    systemctl daemon-reload
    echo "install-timers: removed."
    exit 0
    ;;
  "") ;;
  *) die "unknown option '$1' (expected --status, --uninstall, or nothing)." ;;
esac

[ "$(id -u)" -eq 0 ] || die "must run as root (writes to $UNIT_DIR)."
[ -x "$PYTHON" ]     || die "python not executable at '$PYTHON' - set PYTHON=/path/to/python."
[ -f "$APP_DIR/manage.py" ] || die "no manage.py under '$APP_DIR' - set APP_DIR=/path/to/checkout."
id -u "$RUN_AS" >/dev/null 2>&1 || die "user '$RUN_AS' does not exist - set RUN_AS=someuser."

# Fail loudly now rather than leaving a job that can never load its config.
[ -f "$APP_DIR/.env" ] || echo "install-timers: warning - no .env in $APP_DIR; the jobs will run with the ambient environment only." >&2

echo "install-timers: app=$APP_DIR python=$PYTHON user=$RUN_AS"

for unit in "${UNITS[@]}"; do
    [ -f "$SRC/$unit" ] || die "missing unit template '$SRC/$unit'."
    # '|' as the delimiter because every substitution is a path.
    sed -e "s|@APP_DIR@|$APP_DIR|g" \
        -e "s|@PYTHON@|$PYTHON|g" \
        -e "s|@RUN_AS@|$RUN_AS|g" \
        "$SRC/$unit" > "$UNIT_DIR/$unit"
    chmod 0644 "$UNIT_DIR/$unit"
done

systemctl daemon-reload
systemctl enable --now "${TIMERS[@]}"

echo
systemctl list-timers --all 'fsa-*'
echo
echo "install-timers: done. Logs:  journalctl -u fsa-sync -u fsa-lawyer-report -n 50"
