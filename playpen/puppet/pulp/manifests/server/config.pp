
class pulp::server::config {
    # Database config settings
    $db_name                = $pulp::server::db_name
    $db_seed_list           = $pulp::server::db_seed_list
    $db_operation_retries   = $pulp::server::db_operation_retries
    $db_username            = $pulp::server::db_username
    $db_password            = $pulp::server::db_password
    $db_replica_set         = $pulp::server::db_replica_set

    # Pulp server config settings
    $server_name        = $pulp::server::server_name
    $default_login      = $pulp::server::default_login
    $default_password   = $pulp::server::default_password
    $debugging_mode     = $pulp::server::debugging_mode
    $log_level          = $pulp::server::log_level

    # Authentication settings
    $auth_rsa_key = $pulp::server::auth_rsa_key
    $auth_rsa_pub = $pulp::server::auth_rsa_pub

    # Security settings
    $cacert                     = $pulp::server::cacert
    $cakey                      = $pulp::server::cakey
    $ssl_ca_cert                = $pulp::server::ssl_ca_cert
    $user_cert_expiration       = $pulp::server::user_cert_expiration
    $consumer_cert_expiration   = $pulp::server::consumer_cert_expiration
    $serial_number_path         = $pulp::server::serial_number_path

    # Consumer history settings
    $consumer_history_lifetime = $pulp::server::consumer_history_lifetime

    # Data reaping settings
    $reaper_interval                    = $pulp::server::reaper_interval
    $reap_archived_calls                = $pulp::server::reap_archived_calls
    $reap_repo_sync_history             = $pulp::server::reap_repo_sync_history
    $reap_repo_publish_history          = $pulp::server::reap_repo_publish_history
    $reap_repo_group_publish_history    = $pulp::server::reap_repo_group_publish_history
    $reap_task_status_history           = $pulp::server::reap_task_status_history
    $reap_task_result_history           = $pulp::server::reap_task_result_history

    # Messaging settings
    $msg_url            = $pulp::server::msg_url
    $msg_transport      = $pulp::server::msg_transport
    $msg_auth_enabled   = $pulp::server::msg_auth_enabled
    $msg_cacert         = $pulp::server::msg_cacert
    $msg_clientcert     = $pulp::server::msg_clientcert
    $msg_topic_exchange = $pulp::server::msg_topic_exchange

    # Tasks settings
    $tasks_broker_url   = $pulp::server::tasks_broker_url
    $celery_require_ssl = $pulp::server::celery_require_ssl
    $tasks_cacert       = $pulp::server::tasks_cacert
    $tasks_keyfile      = $pulp::server::tasks_keyfile
    $tasks_certfile     = $pulp::server::tasks_certfile

    # Email settings
    $email_host     = $pulp::server::email_host
    $email_port     = $pulp::server::email_port
    $email_from     = $pulp::server::email_from
    $email_enabled  = $pulp::server::email_enabled

    # Write server.conf file
    file { '/etc/pulp/server.conf':
        content => template('pulp/server.conf.erb'),
        owner   => 'root',
        group   => 'apache',
        mode    => '0644'
    } -> exec { "Migrate DB":
        command => "/usr/bin/pulp-manage-db",
        user    => "apache"
    }
}
