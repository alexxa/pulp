# This manifest installs and configures Pulp's dependencies

class prepulp {

    $packages = [
        'python-qpid-qmf',
        'python-qpid',
        'qpid-cpp-server-store',
        'redhat-lsb',
    ]

    package { $packages:
        ensure => 'installed'
    }

    class {'::mongodb::server':
        smallfiles => true,
        noprealloc => true,
    }

    class {'::qpid::server':
        config_file => '/etc/qpid/qpidd.conf',
        auth => 'no'
    }
}

include prepulp
