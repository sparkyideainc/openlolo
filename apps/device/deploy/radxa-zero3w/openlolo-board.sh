#!/bin/sh
# Lolo Pocket (Radxa ZERO 3W) boot-time tuning: schedutil instead of Armbian's ondemand, which
# kept the cores at 1.416 GHz on a mostly idle box, and the heartbeat LED handed to the button
# daemon. Idempotent.
for policy in /sys/devices/system/cpu/cpufreq/policy*; do
  [ -w "$policy/scaling_governor" ] || continue
  grep -qw schedutil "$policy/scaling_available_governors" 2>/dev/null || continue
  echo schedutil > "$policy/scaling_governor"
done
# The heartbeat LED is the status LED: the button daemon (user openlolo) blinks it in setup mode
# and while a factory reset is armed.
led=/sys/class/leds/green:heartbeat
if [ -w "$led/trigger" ]; then
  echo none > "$led/trigger"
  echo 0 > "$led/brightness"
  chgrp openlolo "$led/brightness" && chmod g+w "$led/brightness"
fi
exit 0
