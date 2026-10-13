-- IntraForge Pay pilot -- initial schema.
--
-- SIMULATED VALUE -- NO CASH VALUE. Every amount in this database is a simulated IF Credit, stored as an integer number
-- of hundredths (amount_minor: 2500 = 25.00 IF Credits) so no arithmetic is ever done in floating point.
--
-- The financial rules are enforced HERE, by PostgreSQL, not only by the application:
--   * every journal transaction balances (sum of debits = sum of credits) and has at least two entries;
--   * a customer's simulated liability account can never go below zero;
--   * journal transactions, journal entries, receipts and audit events can never be updated, deleted or truncated;
--   * a balance is never stored anywhere: it is always the sum of the journal (view account_balances), so a balance
--     cannot change without a valid journal transaction;
--   * a payment request can be paid at most once (UNIQUE journal_transaction_id, and the pending -> paid transition);
--   * a credit allocation can only be approved by someone other than the person who requested it.

CREATE TABLE users (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    username      text NOT NULL UNIQUE CHECK (username ~ '^[a-z0-9._-]{3,40}$'),
    display_name  text NOT NULL CHECK (length(display_name) BETWEEN 1 AND 80),
    role          text NOT NULL CHECK (role IN ('admin', 'supervisor', 'cashier', 'customer')),
    password_hash text NOT NULL,
    active        boolean NOT NULL DEFAULT true,
    created_at    timestamptz NOT NULL DEFAULT now(),
    created_by    uuid REFERENCES users (id)
);

CREATE TABLE sessions (
    token_hash text PRIMARY KEY,
    user_id    uuid NOT NULL REFERENCES users (id),
    csrf_token text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz NOT NULL,
    revoked_at timestamptz
);
CREATE INDEX sessions_user ON sessions (user_id);

CREATE TABLE merchants (
    id                         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    name                       text NOT NULL UNIQUE CHECK (length(name) BETWEEN 1 AND 80),
    active                     boolean NOT NULL DEFAULT true,
    -- The IntraForge Application this shop is registered as (set by the configuration wizard).
    intraforge_application_id  uuid,
    created_at                 timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE merchant_staff (
    merchant_id uuid NOT NULL REFERENCES merchants (id),
    user_id     uuid NOT NULL UNIQUE REFERENCES users (id),
    PRIMARY KEY (merchant_id, user_id)
);

-- ---------------------------------------------------------------------------------------------------------------------
-- Double-entry journal
-- ---------------------------------------------------------------------------------------------------------------------

CREATE TABLE ledger_accounts (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    code        text NOT NULL UNIQUE,
    name        text NOT NULL,
    -- customer_liability: simulated credits the pilot owes a customer (credit-normal).
    -- merchant_payable:   simulated credits the pilot owes a merchant (credit-normal).
    -- issuance_reserve:   the counterpart of every simulated credit allocation (debit-normal).
    kind        text NOT NULL CHECK (kind IN ('customer_liability', 'merchant_payable', 'issuance_reserve')),
    normal_side text NOT NULL CHECK (normal_side IN ('debit', 'credit')),
    customer_id uuid UNIQUE REFERENCES users (id),
    merchant_id uuid UNIQUE REFERENCES merchants (id),
    created_at  timestamptz NOT NULL DEFAULT now(),
    CHECK ((kind = 'customer_liability') = (customer_id IS NOT NULL)),
    CHECK ((kind = 'merchant_payable') = (merchant_id IS NOT NULL)),
    CHECK (normal_side = CASE kind WHEN 'issuance_reserve' THEN 'debit' ELSE 'credit' END)
);
CREATE UNIQUE INDEX ledger_accounts_one_issuance_reserve ON ledger_accounts (kind) WHERE kind = 'issuance_reserve';

CREATE TABLE journal_transactions (
    id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    seq        bigserial NOT NULL UNIQUE,
    kind       text NOT NULL CHECK (kind IN ('allocation', 'purchase')),
    reference  text NOT NULL UNIQUE,
    memo       text NOT NULL DEFAULT '',
    created_by uuid NOT NULL REFERENCES users (id),
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE journal_entries (
    id             bigserial PRIMARY KEY,
    transaction_id uuid NOT NULL REFERENCES journal_transactions (id),
    account_id     uuid NOT NULL REFERENCES ledger_accounts (id),
    side           text NOT NULL CHECK (side IN ('debit', 'credit')),
    amount_minor   bigint NOT NULL CHECK (amount_minor > 0)
);
CREATE INDEX journal_entries_account ON journal_entries (account_id);
CREATE INDEX journal_entries_transaction ON journal_entries (transaction_id);

CREATE VIEW account_balances AS
SELECT a.id AS account_id,
       a.code,
       a.name,
       a.kind,
       a.normal_side,
       a.customer_id,
       a.merchant_id,
       COALESCE(SUM(CASE WHEN e.side = 'debit' THEN e.amount_minor ELSE 0 END), 0)::bigint  AS debits_minor,
       COALESCE(SUM(CASE WHEN e.side = 'credit' THEN e.amount_minor ELSE 0 END), 0)::bigint AS credits_minor,
       (CASE WHEN a.normal_side = 'credit'
             THEN COALESCE(SUM(CASE WHEN e.side = 'credit' THEN e.amount_minor ELSE -e.amount_minor END), 0)
             ELSE COALESCE(SUM(CASE WHEN e.side = 'debit' THEN e.amount_minor ELSE -e.amount_minor END), 0)
        END)::bigint AS balance_minor
FROM ledger_accounts a
LEFT JOIN journal_entries e ON e.account_id = a.id
GROUP BY a.id;

-- A transaction must balance and have at least two entries. Checked at COMMIT (deferred), once per transaction row
-- and once per entry row, so a transaction can be written header-first and still be validated as a whole.
CREATE FUNCTION ifpay_assert_balanced(tx uuid) RETURNS void LANGUAGE plpgsql AS $$
DECLARE
    n integer;
    d bigint;
    c bigint;
BEGIN
    SELECT count(*),
           COALESCE(SUM(amount_minor) FILTER (WHERE side = 'debit'), 0),
           COALESCE(SUM(amount_minor) FILTER (WHERE side = 'credit'), 0)
      INTO n, d, c
      FROM journal_entries WHERE transaction_id = tx;
    IF n < 2 THEN
        RAISE EXCEPTION 'journal transaction % has % entries; at least two are required', tx, n
            USING ERRCODE = 'check_violation';
    END IF;
    IF d <> c THEN
        RAISE EXCEPTION 'journal transaction % does not balance (debits %, credits %)', tx, d, c
            USING ERRCODE = 'check_violation';
    END IF;
END $$;

CREATE FUNCTION ifpay_check_transaction_balanced() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    PERFORM ifpay_assert_balanced(NEW.id);
    RETURN NULL;
END $$;

CREATE FUNCTION ifpay_check_entry_balanced() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    PERFORM ifpay_assert_balanced(NEW.transaction_id);
    RETURN NULL;
END $$;

CREATE CONSTRAINT TRIGGER journal_transactions_balanced
    AFTER INSERT ON journal_transactions DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION ifpay_check_transaction_balanced();

CREATE CONSTRAINT TRIGGER journal_entries_balanced
    AFTER INSERT ON journal_entries DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION ifpay_check_entry_balanced();

-- A customer's simulated liability balance can never be negative. The account row is locked first, so two concurrent
-- payments by the same customer are serialised here even if the application forgot to lock; each statement in this
-- (volatile) function then reads the latest committed journal.
CREATE FUNCTION ifpay_check_customer_not_overdrawn() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    k text;
    bal bigint;
BEGIN
    SELECT kind INTO k FROM ledger_accounts WHERE id = NEW.account_id FOR UPDATE;
    IF k <> 'customer_liability' THEN
        RETURN NULL;
    END IF;
    SELECT COALESCE(SUM(CASE WHEN side = 'credit' THEN amount_minor ELSE -amount_minor END), 0)
      INTO bal FROM journal_entries WHERE account_id = NEW.account_id;
    IF bal < 0 THEN
        RAISE EXCEPTION 'insufficient simulated balance on account % (would be %)', NEW.account_id, bal
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NULL;
END $$;

CREATE CONSTRAINT TRIGGER journal_entries_customer_not_overdrawn
    AFTER INSERT ON journal_entries DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW WHEN (NEW.side = 'debit')
    EXECUTE FUNCTION ifpay_check_customer_not_overdrawn();

-- Immutable records: no UPDATE, DELETE or TRUNCATE, ever.
CREATE FUNCTION ifpay_forbid_change() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION '% is immutable: % is not allowed', TG_TABLE_NAME, TG_OP USING ERRCODE = 'insufficient_privilege';
END $$;

-- ---------------------------------------------------------------------------------------------------------------------
-- Maker/checker credit allocations
-- ---------------------------------------------------------------------------------------------------------------------

CREATE TABLE credit_allocations (
    id                     uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    customer_id            uuid NOT NULL REFERENCES users (id),
    amount_minor           bigint NOT NULL CHECK (amount_minor > 0 AND amount_minor <= 100000),
    reason                 text NOT NULL CHECK (length(reason) BETWEEN 1 AND 200),
    status                 text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'approved', 'rejected')),
    requested_by           uuid NOT NULL REFERENCES users (id),
    requested_at           timestamptz NOT NULL DEFAULT now(),
    decided_by             uuid REFERENCES users (id),
    decided_at             timestamptz,
    decision_note          text NOT NULL DEFAULT '',
    journal_transaction_id uuid UNIQUE REFERENCES journal_transactions (id),
    CHECK (decided_by IS NULL OR decided_by <> requested_by),
    CHECK ((status = 'pending') = (decided_by IS NULL AND decided_at IS NULL)),
    CHECK ((status = 'approved') = (journal_transaction_id IS NOT NULL))
);

-- Only pending -> approved/rejected, and nothing but the decision columns may change.
CREATE FUNCTION ifpay_allocation_transition() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF OLD.status <> 'pending' THEN
        RAISE EXCEPTION 'credit allocation % is already %', OLD.id, OLD.status USING ERRCODE = 'insufficient_privilege';
    END IF;
    IF NEW.status = 'pending' THEN
        RAISE EXCEPTION 'a pending credit allocation can only be approved or rejected' USING ERRCODE = 'insufficient_privilege';
    END IF;
    IF NEW.customer_id <> OLD.customer_id OR NEW.amount_minor <> OLD.amount_minor OR NEW.reason <> OLD.reason
       OR NEW.requested_by <> OLD.requested_by OR NEW.requested_at <> OLD.requested_at THEN
        RAISE EXCEPTION 'a credit allocation request cannot be edited' USING ERRCODE = 'insufficient_privilege';
    END IF;
    RETURN NEW;
END $$;

CREATE TRIGGER credit_allocations_transition BEFORE UPDATE ON credit_allocations
    FOR EACH ROW EXECUTE FUNCTION ifpay_allocation_transition();
CREATE TRIGGER credit_allocations_no_delete BEFORE DELETE ON credit_allocations
    FOR EACH ROW EXECUTE FUNCTION ifpay_forbid_change();

-- ---------------------------------------------------------------------------------------------------------------------
-- QR checkout
-- ---------------------------------------------------------------------------------------------------------------------

CREATE TABLE payment_requests (
    id                     uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    merchant_id            uuid NOT NULL REFERENCES merchants (id),
    amount_minor           bigint NOT NULL CHECK (amount_minor > 0 AND amount_minor <= 100000),
    created_by             uuid NOT NULL REFERENCES users (id),
    created_at             timestamptz NOT NULL DEFAULT now(),
    expires_at             timestamptz NOT NULL,
    merchant_verified      boolean NOT NULL,
    status                 text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'paid', 'cancelled')),
    paid_by                uuid REFERENCES users (id),
    paid_at                timestamptz,
    journal_transaction_id uuid UNIQUE REFERENCES journal_transactions (id),
    CHECK (expires_at > created_at),
    CHECK ((status = 'paid') = (paid_by IS NOT NULL AND paid_at IS NOT NULL AND journal_transaction_id IS NOT NULL))
);
CREATE INDEX payment_requests_merchant_created ON payment_requests (merchant_id, created_at DESC);

CREATE FUNCTION ifpay_payment_request_transition() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF OLD.status <> 'pending' THEN
        RAISE EXCEPTION 'payment request % is already %', OLD.id, OLD.status USING ERRCODE = 'insufficient_privilege';
    END IF;
    IF NEW.status = 'pending' THEN
        RAISE EXCEPTION 'a pending payment request can only become paid or cancelled' USING ERRCODE = 'insufficient_privilege';
    END IF;
    IF NEW.merchant_id <> OLD.merchant_id OR NEW.amount_minor <> OLD.amount_minor OR NEW.created_by <> OLD.created_by
       OR NEW.created_at <> OLD.created_at OR NEW.expires_at <> OLD.expires_at
       OR NEW.merchant_verified <> OLD.merchant_verified THEN
        RAISE EXCEPTION 'a payment request cannot be edited' USING ERRCODE = 'insufficient_privilege';
    END IF;
    IF NEW.status = 'paid' AND OLD.expires_at <= now() THEN
        RAISE EXCEPTION 'payment request % has expired', OLD.id USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END $$;

CREATE TRIGGER payment_requests_transition BEFORE UPDATE ON payment_requests
    FOR EACH ROW EXECUTE FUNCTION ifpay_payment_request_transition();
CREATE TRIGGER payment_requests_no_delete BEFORE DELETE ON payment_requests
    FOR EACH ROW EXECUTE FUNCTION ifpay_forbid_change();

-- ---------------------------------------------------------------------------------------------------------------------
-- Receipts and documents
-- ---------------------------------------------------------------------------------------------------------------------

CREATE SEQUENCE receipt_number_seq;

CREATE TABLE receipts (
    id                     uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    receipt_number         text NOT NULL UNIQUE,
    journal_transaction_id uuid NOT NULL UNIQUE REFERENCES journal_transactions (id),
    payment_request_id     uuid NOT NULL UNIQUE REFERENCES payment_requests (id),
    customer_id            uuid NOT NULL REFERENCES users (id),
    merchant_id            uuid NOT NULL REFERENCES merchants (id),
    amount_minor           bigint NOT NULL CHECK (amount_minor > 0),
    issued_at              timestamptz NOT NULL,
    body                   jsonb NOT NULL,
    body_sha256            text NOT NULL CHECK (body_sha256 ~ '^[0-9a-f]{64}$')
);

-- Successful uploads of a receipt or a supporting document to the IntraForge bucket (failures go to the audit log).
CREATE TABLE intraforge_uploads (
    id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    receipt_id         uuid NOT NULL REFERENCES receipts (id),
    kind               text NOT NULL CHECK (kind IN ('receipt', 'supporting_document')),
    filename           text NOT NULL,
    mime_type          text NOT NULL,
    size_bytes         bigint NOT NULL CHECK (size_bytes >= 0),
    sha256             text NOT NULL CHECK (sha256 ~ '^[0-9a-f]{64}$'),
    intraforge_file_id uuid NOT NULL UNIQUE,
    bucket_id          uuid NOT NULL,
    uploaded_by        uuid REFERENCES users (id),
    uploaded_at        timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX intraforge_uploads_one_receipt_file ON intraforge_uploads (receipt_id) WHERE kind = 'receipt';

-- ---------------------------------------------------------------------------------------------------------------------
-- Audit log (append-only)
-- ---------------------------------------------------------------------------------------------------------------------

CREATE TABLE audit_events (
    id             bigserial PRIMARY KEY,
    occurred_at    timestamptz NOT NULL DEFAULT now(),
    actor_id       uuid REFERENCES users (id),
    actor_username text NOT NULL DEFAULT '',
    action         text NOT NULL,
    result         text NOT NULL CHECK (result IN ('success', 'denied', 'error')),
    subject_type   text NOT NULL DEFAULT '',
    subject_id     text NOT NULL DEFAULT '',
    detail         jsonb NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX audit_events_occurred ON audit_events (occurred_at DESC);

-- ---------------------------------------------------------------------------------------------------------------------
-- IntraForge integration (non-secret configuration only; the credential never enters this database)
-- ---------------------------------------------------------------------------------------------------------------------

CREATE TABLE integration_settings (
    id         integer PRIMARY KEY CHECK (id = 1),
    config     jsonb NOT NULL,
    updated_by uuid REFERENCES users (id),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE integration_status (
    id         integer PRIMARY KEY CHECK (id = 1),
    ok         boolean NOT NULL,
    checked_at timestamptz NOT NULL,
    detail     jsonb NOT NULL DEFAULT '{}'::jsonb
);

-- Immutability triggers for the financial and audit record tables.
DO $$
DECLARE
    t text;
BEGIN
    FOREACH t IN ARRAY ARRAY['journal_transactions', 'journal_entries', 'receipts', 'audit_events', 'intraforge_uploads']
    LOOP
        EXECUTE format('CREATE TRIGGER %I BEFORE UPDATE OR DELETE ON %I FOR EACH ROW EXECUTE FUNCTION ifpay_forbid_change()',
                       t || '_immutable', t);
        EXECUTE format('CREATE TRIGGER %I BEFORE TRUNCATE ON %I FOR EACH STATEMENT EXECUTE FUNCTION ifpay_forbid_change()',
                       t || '_no_truncate', t);
    END LOOP;
END $$;

-- The one issuance reserve account.
INSERT INTO ledger_accounts (code, name, kind, normal_side)
VALUES ('ISSUANCE-RESERVE', 'Simulated credit issuance reserve', 'issuance_reserve', 'debit');
