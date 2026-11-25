"""dciab status: where a site's bootstrap stands, read-only.

    dciab status <config> [--state DIR] [--output text|json]

Each check reports ok, missing, down or unknown (with why), and changes nothing:
  services   the Day 0 pod (dciab services), when the config has spec.hypervisors, and
             registry.<domain> resolving on each DNS service IP
  dns        the same pod run DNS-only by dciab seed, for configs without spec.hypervisors
  pki        the platform CA and the registry certificate in <state>/pki, and when it expires
  registry   the seed registry container, and its /v2/ answering with the trust bundle
  bootstrap  the kind cluster's API (seed's kubeconfig), Cluster API and the ClusterClasses
  mgmt       MGMT's Cluster: on the bootstrap cluster before the move, on MGMT after it

Exit code: 0 when every check that applies is ok, 1 otherwise, 2 could not run.
Container checks need root (rootful podman); without it they report unknown, except services
and dns, which still check the DNS answer from the network.
"""
