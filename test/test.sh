#!/bin/sh

export PYTHONPATH="$(realpath $(dirname $0))":"$(realpath $(dirname $0)/..)":"$PYTHONPATH"

cd "$(realpath $(dirname $0))"
python2 test_shrinklvm.py -v
python2 test_mdraid.py -v
