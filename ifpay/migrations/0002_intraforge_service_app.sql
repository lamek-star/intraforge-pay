-- IntraForge Pay runs inside IntraForge as a service application (IntraForge ADR-0018).
--
-- Shop staff are IntraForge people: they open Pay from IntraForge's Apps page, IntraForge decides who they are and what
-- they may do (ifpay.* capabilities), and Pay keeps one row per person only so the journal, receipts and audit log can
-- name them (role 'staff', keyed by their IntraForge user id, no password). Customers keep their own wallet accounts.
-- The earlier pilot's own staff accounts (admin, supervisor, cashier) can no longer sign in; they are deactivated, not
-- deleted, because the journal and audit log refer to them.

ALTER TABLE users ADD COLUMN intraforge_user_id uuid UNIQUE;
ALTER TABLE users ALTER COLUMN password_hash DROP NOT NULL;
ALTER TABLE users DROP CONSTRAINT users_role_check;
ALTER TABLE users ADD CONSTRAINT users_role_check
    CHECK (role IN ('staff', 'customer', 'admin', 'supervisor', 'cashier'));
ALTER TABLE users ADD CONSTRAINT users_staff_are_intraforge_people
    CHECK ((role = 'staff') = (intraforge_user_id IS NOT NULL));
ALTER TABLE users ADD CONSTRAINT users_customers_have_a_password
    CHECK (role = 'staff' OR password_hash IS NOT NULL);
-- A display name from IntraForge can be longer than the pilot's 80 characters.
ALTER TABLE users DROP CONSTRAINT users_display_name_check;
ALTER TABLE users ADD CONSTRAINT users_display_name_check CHECK (length(display_name) BETWEEN 1 AND 200);
UPDATE users SET active = false WHERE role IN ('admin', 'supervisor', 'cashier');
UPDATE sessions SET revoked_at = now()
 WHERE revoked_at IS NULL AND user_id IN (SELECT id FROM users WHERE role IN ('admin', 'supervisor', 'cashier'));

-- The shop is the IntraForge Pay instance (merchants.intraforge_application_id = the IntraForge service app id).
ALTER TABLE merchants DROP CONSTRAINT merchants_name_check;
ALTER TABLE merchants ADD CONSTRAINT merchants_name_check CHECK (length(name) BETWEEN 1 AND 200);

-- Receipts are stored in the instance's own IntraForge bucket through the internal files API, which does not name
-- the bucket; the IntraForge file id is the reference.
ALTER TABLE intraforge_uploads ALTER COLUMN bucket_id DROP NOT NULL;

-- The earlier pilot's IntraForge connection wizard (application credential + bucket id) is gone: IntraForge itself
-- installs, identifies and provisions Pay now.
DROP TABLE integration_settings;

-- Audit events relayed to IntraForge's audit log (at least once; IntraForge records each event id once).
-- audit_events itself stays append-only, so the relay is tracked here.
CREATE TABLE audit_relay (
    event_id   bigint PRIMARY KEY REFERENCES audit_events (id),
    relayed_at timestamptz NOT NULL DEFAULT now()
);

-- The key that signs QR payment codes, kept with the ledger (in IntraForge's backups), not in a container file.
CREATE TABLE signing_keys (
    id         integer PRIMARY KEY CHECK (id = 1),
    key        bytea NOT NULL CHECK (length(key) = 32),
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TRIGGER signing_keys_immutable BEFORE UPDATE OR DELETE ON signing_keys
    FOR EACH ROW EXECUTE FUNCTION ifpay_forbid_change();
