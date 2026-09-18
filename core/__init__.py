"""Process-lifetime plumbing: the bounded sample queue (BIO-6), the shared
`SessionContext` slot that tells every device worker whether a recording is
currently open, and the per-device orchestration that ties a transport, its
queue, and ingest together (BIO-6/BIO-9). See device_worker.py."""
