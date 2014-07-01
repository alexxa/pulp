# This class installs and configures a Pulp server from the latest scratch build

class pulp_server {
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
