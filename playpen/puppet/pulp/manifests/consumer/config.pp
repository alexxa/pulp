# This is a private class and should be be called directly. Use pulp::consumer instead

class pulp::consumer::config {
    # Server
    $pulp_server        = $pulp::consumer::pulp_server
    $pulp_port          = $pulp::consumer::pulp_port
    $pulp_api_prefix    = $pulp::consumer::pulp_api_prefix
    $pulp_rsa_pub       = $pulp::consumer::pulp_rsa_pub

    # Authentication
    $consumer_rsa_key = $pulp::consumer::consumer_rsa_key
    $consumer_rsa_pub = $pulp::consumer::consumer_rsa_pub

    # Client role
    $client_role = $pulp::consumer::client_role

    # Filesystem
    $extensions_dir    = $pulp::consumer::extensions_dir
    $repo_file         = $pulp::consumer::repo_file
    $mirror_list_dir   = $pulp::consumer::mirror_list_dir
    $gpg_keys_dir      = $pulp::consumer::gpg_keys_dir
    $cert_dir          = $pulp::consumer::cert_dir
    $id_cert_dir       = $pulp::consumer::id_cert_dir
    $id_cert_filename  = $pulp::consumer::id_cert_filename

    # Reboot
    $reboot         = $pulp::consumer::reboot
    $reboot_delay   = $pulp::consumer::reboot_delay

    # Logging
    $log_filename       = $pulp::consumer::log_filename
    $call_log_filename  = $pulp::consumer::call_log_filename

    # Output
    $poll_frequency = $pulp::consumer::poll_frequency
    $color_output   = $pulp::consumer::color_output
    $wrap_terminal  = $pulp::consumer::wrap_terminal
    $wrap_width     = $pulp::consumer::wrap_width

    # Messaging
    $msg_scheme        = $pulp::consumer::msg_scheme
    $msg_host          = $pulp::consumer::msg_host
    $msg_port          = $pulp::consumer::msg_port
    $msg_transport     = $pulp::consumer::msg_transport
    $msg_cacert        = $pulp::consumer::msg_cacert
    $msg_clientcert    = $pulp::consumer::msg_clientcert

    # Profile
    $profile_minutes = $pulp::consumer::profile_minutes


    # Write consumer.conf file
    file { '/etc/pulp/consumer/consumer.conf':
        content => template('pulp/consumer.conf.erb'),
        owner   => 'root',
        group   => 'root',
        mode    => '0644'
    }
}
