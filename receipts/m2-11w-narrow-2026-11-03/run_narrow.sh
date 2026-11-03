#!/bin/bash
E=/tmp/claude-0/m2-11w-env
for side in head base; do
  if [ $side = head ]; then cd /home/user/machine/run/m2-11w; else cd /tmp/claude-0/base-2351; fi
  timeout 1500 $E/venv/bin/python -u -m pytest -q -p no:cacheprovider -rfE --continue-on-collection-errors $(cat $E/narrow.txt | tr '\n' ' ') > $E/narrow-$side.out 2>&1
  echo "exit $?" >> $E/narrow-$side.out
done
echo DONE > $E/narrow_done
