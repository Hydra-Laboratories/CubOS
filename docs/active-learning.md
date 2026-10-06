# Active learning lives in Ursa Learning

Generic optimization campaigns, color matching and overnight queues run in the separate Ursa Learning application. CubOS owns protocol execution, hardware controls, calibrated setup, measurements and inventory. The learning application calls the CubOS HTTP API and stores its own optimization records.

See [External API Clients](external-clients.md) for the execution, reservation and evidence contract. Existing records are preserved on disk; upgrading CubOS does not migrate or delete them. Keep a backup of the original campaign histories and application version so earlier campaigns remain reviewable. Automatic history import into Ursa Learning is not supplied. Inspect prior native runs and reconcile physical inventory before starting new campaigns.
