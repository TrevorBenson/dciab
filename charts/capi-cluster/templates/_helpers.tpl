{{- /* Checks the values schema can't express. Fails the render with a clear message. */ -}}
{{- define "capi-cluster.validate" -}}
{{- $seen := dict }}
{{- range .Values.workerPools }}
{{- if hasKey $seen .name }}
{{- fail (printf "workerPools: the name %q is used more than once; each pool needs its own name" .name) }}
{{- end }}
{{- $_ := set $seen .name true }}
{{- end }}
{{- if eq .Values.class "proxmox" }}
{{- if not (has .Values.proxmox.templateNode .Values.proxmox.allowedNodes) }}
{{- fail (printf "proxmox.templateNode %q must be one of proxmox.allowedNodes %v" .Values.proxmox.templateNode .Values.proxmox.allowedNodes) }}
{{- end }}
{{- end }}
{{- end }}

{{- define "capi-cluster.labels" -}}
cluster.x-k8s.io/cluster-name: {{ .Values.name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/part-of: dciab
{{- end }}

{{- /* The first and last address of an "a-b" range. */ -}}
{{- define "capi-cluster.rangeStart" -}}{{ index (splitList "-" .) 0 }}{{- end }}
{{- define "capi-cluster.rangeEnd" -}}{{ index (splitList "-" .) 1 }}{{- end }}

{{- /* IPPool + Metal3DataTemplate for one Metal3 role. Args: dict "root" $ "role" <name> "network" <net> */ -}}
{{- define "capi-cluster.metal3Role" -}}
{{- $name := printf "%s-%s" .root.Values.name .role }}
---
apiVersion: ipam.metal3.io/v1alpha1
kind: IPPool
metadata:
  name: {{ $name }}
  labels:
    {{- include "capi-cluster.labels" .root | nindent 4 }}
spec:
  clusterName: {{ .root.Values.name }}
  namePrefix: {{ $name }}
  pools:
    - start: {{ include "capi-cluster.rangeStart" .network.nodeIPRange }}
      end: {{ include "capi-cluster.rangeEnd" .network.nodeIPRange }}
  prefix: {{ .network.prefix }}
  gateway: {{ .network.gateway }}
  dnsServers: {{ toJson .root.Values.nodes.dnsServers }}
---
apiVersion: infrastructure.cluster.x-k8s.io/v1beta2
kind: Metal3DataTemplate
metadata:
  name: {{ $name }}
  labels:
    {{- include "capi-cluster.labels" .root | nindent 4 }}
spec:
  clusterName: {{ .root.Values.name }}
  networkData:
    links:
      ethernets:
        - id: nic0
          type: phy
          macAddress:
            # Each BareMetalHost carries its boot NIC's MAC in this annotation.
            fromAnnotation:
              object: baremetalhost
              annotation: dciab.io/boot-mac
    networks:
      ipv4:
        - id: nic0-ipv4
          link: nic0
          fromPoolRef:
            apiGroup: ipam.metal3.io
            kind: IPPool
            name: {{ $name }}
          routes:
            - network: 0.0.0.0
              prefix: 0
              gateway:
                fromPoolRef:
                  apiGroup: ipam.metal3.io
                  kind: IPPool
                  name: {{ $name }}
    services:
      dns: {{ toJson .root.Values.nodes.dnsServers }}
{{- end }}
