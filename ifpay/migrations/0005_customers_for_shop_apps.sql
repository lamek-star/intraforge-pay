-- Customer wallets opened for another application in the same shop (IntraForge ADR-0022, the Movie Shop portal).
--
-- When the shop owner approves a portal customer in IntraForge ("Add to IF Pay"), IntraForge asks Pay -- on behalf of
-- a person who holds ifpay.customers.manage -- to open an ordinary customer wallet. The wallet remembers which
-- application asked and for which of its records, so asking twice opens one wallet, and that application can later
-- see the wallet it opened (and no other).
--
-- Pay chooses a one-time first password and returns it once; the customer must choose their own before the wallet
-- does anything else (must_change_password).

ALTER TABLE users ADD COLUMN source_app_id uuid;
ALTER TABLE users ADD COLUMN source_external_id text
    CHECK (source_external_id IS NULL OR length(source_external_id) BETWEEN 1 AND 120);
ALTER TABLE users ADD COLUMN must_change_password boolean NOT NULL DEFAULT false;

ALTER TABLE users ADD CONSTRAINT users_source_pair CHECK ((source_app_id IS NULL) = (source_external_id IS NULL));
ALTER TABLE users ADD CONSTRAINT users_source_is_customer CHECK (source_app_id IS NULL OR role = 'customer');
ALTER TABLE users ADD CONSTRAINT users_must_change_is_customer CHECK (NOT must_change_password OR role = 'customer');

CREATE UNIQUE INDEX users_source ON users (source_app_id, source_external_id) WHERE source_app_id IS NOT NULL;
