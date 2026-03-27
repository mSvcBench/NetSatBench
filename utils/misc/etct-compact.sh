#!/bin/bash
# This script compacts the etcd database and defragments it to free up disk space. 
# It also disarms any alarms that may be present.

REV=$(etcdctl endpoint status -w json | jq -r '.[0].Status.header.revision')
etcdctl compact "$REV"
etcdctl defrag
etcdctl alarm disarm