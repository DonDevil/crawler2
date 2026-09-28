"""crawler2 storage layer (P2): Scylla repositories, object store, outbox.

Layering: ``antipiracy_contracts`` (models) → ``crawler2.storage.repositories``
(interfaces, no driver types) → ``crawler2.storage.scylla`` /
``crawler2.storage.objectstore`` (implementations). Callers depend on the
interfaces; nothing outside this package sees CQL.
"""
