#!/bin/sh
set -eu

config_file=/tmp/secman/config/secman.txt
if [ ! -r "$config_file" ]; then
    echo "Secman file list is missing or unreadable: $config_file" >&2
    exit 1
fi

# Secman supplies whitespace-separated absolute paths, as in the SIGMA template.
set -f
checked_files=$(cat "$config_file")
time_counter=0
while :; do
    all_present=1
    for file in $checked_files; do
        if [ ! -f "$file" ]; then
            all_present=0
            break
        fi
    done
    [ "$all_present" -eq 1 ] && break
    sleep 1
    time_counter=$((time_counter + 1))
    echo "Waiting for Secman files: $time_counter s."
done
