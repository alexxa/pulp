#!/usr/bin/env bash
# This script expects to run within the Jenkins Build Process
# The purpose of the script is to run the test suite for each project and save the results so the build
# server can parse them.

echo "Running the tests"
set -x
# Jenkins isn't setting the workspace properly on slave nodes so resetting it here
WORKSPACE="$(readlink -f $( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )/../../../)"

cd ${WORKSPACE}/pulp/playpen/deploy
openstack/deploy-environment.py --os1-key ${OS1_KEY_NAME} --key-file ${KEY_FILE} --repository ${PULP_REPOSITORY} \
--server-puppet puppet/pulp-server.pp --consumer-puppet puppet/pulp-consumer.pp --distribution=${DISTRIBUTION} \
--consumer-hostname=${CONSUMER_HOSTNAME} --server-hostname=${SERVER_HOSTNAME} --tester-hostname=${TESTER_HOSTNAME}

