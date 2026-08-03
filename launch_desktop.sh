#!/usr/bin/env bash
set -u

project_dir="/home/kamjin/projects/pyVideoTrans"
log_file="$project_dir/logs/desktop-launch.log"
mkdir -p "$project_dir/logs"
printf '\n[%s] desktop launcher started\n' "$(date '+%F %T')" >> "$log_file"
cd "$project_dir" || exit 1
"$project_dir/.venv/bin/python" "$project_dir/start_local.py" --lang zh >> "$log_file" 2>&1
status=$?
printf '[%s] desktop launcher exited with status %s\n' "$(date '+%F %T')" "$status" >> "$log_file"
exit "$status"
