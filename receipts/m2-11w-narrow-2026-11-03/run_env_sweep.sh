#!/bin/bash
E=/tmp/claude-0/m2-11w-env
for side in head base; do
  if [ $side = head ]; then cd /home/user/machine/run/m2-11w; else cd /tmp/claude-0/base-2351; fi
  timeout 2400 $E/venv/bin/python -u -m pytest -q -p no:cacheprovider -rfE --continue-on-collection-errors $(cat /tmp/claude-0/step5/fail_files.txt) > $E/$side.out 2>&1
  echo "exit $?" >> $E/$side.out
done
echo DONE > $E/done
