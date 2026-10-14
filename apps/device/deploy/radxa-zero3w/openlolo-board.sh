#!/bin/sh
# Lolo Pocket (Radxa ZERO 3W) boot-time tuning: schedutil instead of Armbian's ondemand, which
# kept the cores at 1.416 GHz on a mostly idle box, and the heartbeat LED off. Idempotent.
for policy in /sys/devices/system/cpu/cpufreq/policy*; do
  [ -w "$policy/scaling_governor" ] || continue
  grep -qw schedutil "$policy/scaling_available_governors" 2>/dev/null || continue
  echo schedutil > "$policy/scaling_governor"
done
led=/sys/class/leds/green:heartbeat/trigger
[ ! -w "$led" ] || echo none > "$led"
exit 0
