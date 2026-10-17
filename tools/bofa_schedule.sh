#!/bin/zsh
# Run the BofA pull on the 28th of every month at 9 AM (or at the next wake, if the Mac was asleep then).
#   tools/bofa_schedule.sh install    |    tools/bofa_schedule.sh remove
cd "${0:A:h:h}"
source tools/env.sh || exit 1
LABEL=com.financialanalyst.bofa-monthly
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
if [[ "$1" == remove ]]; then
  launchctl bootout "gui/$UID/$LABEL" 2>/dev/null; rm -f "$PLIST" && echo "Monthly BofA pull removed."; exit 0
fi
mkdir -p "${PLIST:h}"
cat > "$PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key><array><string>$PY</string><string>$PWD/tools/bofa_fetch.py</string></array>
  <key>WorkingDirectory</key><string>$PWD</string>
  <key>StartCalendarInterval</key><dict><key>Day</key><integer>28</integer><key>Hour</key><integer>9</integer><key>Minute</key><integer>0</integer></dict>
  <key>StandardOutPath</key><string>$DATA/bank_fetch/bofa_schedule.log</string>
  <key>StandardErrorPath</key><string>$DATA/bank_fetch/bofa_schedule.log</string>
</dict></plist>
PLIST
launchctl bootout "gui/$UID/$LABEL" 2>/dev/null
launchctl bootstrap "gui/$UID" "$PLIST" && echo "Scheduled: the BofA pull runs on the 28th of each month at 9 AM."
