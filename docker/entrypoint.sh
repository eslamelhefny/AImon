#!/bin/sh
# Serves the dashboard and runs the collector loop.
# The loop wakes every TICK seconds; each account is only polled when its
# own interval (config.yaml `interval:`) has passed.
mkdir -p /data
cp /app/config.example.yaml /data/config.example.yaml

# Create config.yaml on first start, or upgrade it while it is still an
# untouched copy of a shipped example (so new widgets appear automatically).
OLD_EXAMPLES="e47f07cb84c023f8cd3aa6ec818f9fce 1cc198d1cf6a4fad84473ac9131c2fcc"
NEW_SUM=$(md5sum /app/config.example.yaml | cut -d' ' -f1)
if [ ! -f /data/config.yaml ]; then
  cp /app/config.example.yaml /data/config.yaml
  echo "$NEW_SUM" > /data/.example_sum
else
  CUR=$(md5sum /data/config.yaml | cut -d' ' -f1)
  PREV=$(cat /data/.example_sum 2>/dev/null)
  case " $OLD_EXAMPLES $PREV " in
    *" $CUR "*)
      if [ "$CUR" != "$NEW_SUM" ]; then
        cp /app/config.example.yaml /data/config.yaml
        echo "$NEW_SUM" > /data/.example_sum
        echo "config.yaml was unedited: upgraded to the new example"
      fi ;;
  esac
fi

(
  while true; do
    python /app/aimeter.py -c /data/config.yaml collect || true
    sleep "${TICK:-60}"
  done
) &
exec python /app/aimeter.py -c /data/config.yaml serve --host 0.0.0.0 --port 8787 --no-collect
