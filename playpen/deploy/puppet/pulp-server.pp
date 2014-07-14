# This class installs and configures a Pulp server the repository specified
# by the pulp_repo fact. This fact is retrieved from the FACTER_PULP_REPO
# environment variable

class pulp_server {
    include stdlib

    service { 'iptables':
        enable => false,
        ensure => 'stopped'
    } -> service { 'mongod':
        enable => true,
        ensure => 'running'
    } -> service { 'qpidd':
        enable => true,
        ensure => 'running'
    } -> class {'::pulp::globals':
        repo_baseurl => $::pulp_repo
    } -> class {'::pulp::server':
    }
}

include pulp_server
