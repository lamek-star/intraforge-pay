-- Refunds and cashback (pilot: simulated IF Credits, treated with the same rules as money).
--
-- Refunds: a staff member asks for a refund of a paid payment (all or part of it); a DIFFERENT person approves or
-- rejects it. Only an approval posts to the journal: Debit the shop's payable, Credit the customer's wallet. The total
-- approved for one payment can never exceed what was paid.
--
-- Cashback: the shop pays back a percentage of each wallet payment into the customer's wallet, from its own payable
-- (Debit merchant payable, Credit customer liability), so no credit is created. The rate and the daily cap per
-- customer are changed by proposal: one person proposes, a different person (the supervisor) approves. A refund takes
-- back the matching share of that payment's cashback.
--
-- As in 0001, PostgreSQL enforces these rules itself, whatever the application does.

ALTER TABLE journal_transactions DROP CONSTRAINT IF EXISTS journal_transactions_kind_check;
ALTER TABLE journal_transactions ADD CONSTRAINT journal_transactions_kind_check
    CHECK (kind IN ('allocation', 'purchase', 'cashback', 'refund'));

-- ---------------------------------------------------------------------------------------------------------------------
-- Cashback rate: proposed by one person, approved by another. The rate in force is the latest approved proposal.
-- ---------------------------------------------------------------------------------------------------------------------

CREATE TABLE cashback_rate_changes (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    rate_bp         integer NOT NULL CHECK (rate_bp BETWEEN 0 AND 2000),          -- basis points: 200 = 2.00 %, max 20 %
    daily_cap_minor bigint NOT NULL CHECK (daily_cap_minor BETWEEN 0 AND 100000), -- per customer per day, max 1,000.00
    reason          text NOT NULL CHECK (length(reason) BETWEEN 1 AND 200),
    status          text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'approved', 'rejected')),
    requested_by    uuid NOT NULL REFERENCES users (id),
    requested_at    timestamptz NOT NULL DEFAULT now(),
    decided_by      uuid REFERENCES users (id),
    decided_at      timestamptz,
    decision_note   text NOT NULL DEFAULT '',
    CHECK (decided_by IS NULL OR decided_by <> requested_by),
    CHECK ((status = 'pending') = (decided_by IS NULL AND decided_at IS NULL))
);
CREATE INDEX cashback_rate_changes_approved ON cashback_rate_changes (decided_at DESC) WHERE status = 'approved';

CREATE FUNCTION ifpay_cashback_rate_transition() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF OLD.status <> 'pending' THEN
        RAISE EXCEPTION 'cashback rate change % is already %', OLD.id, OLD.status USING ERRCODE = 'insufficient_privilege';
    END IF;
    IF NEW.status = 'pending' THEN
        RAISE EXCEPTION 'a pending cashback rate change can only be approved or rejected' USING ERRCODE = 'insufficient_privilege';
    END IF;
    IF NEW.rate_bp <> OLD.rate_bp OR NEW.daily_cap_minor <> OLD.daily_cap_minor OR NEW.reason <> OLD.reason
       OR NEW.requested_by <> OLD.requested_by OR NEW.requested_at <> OLD.requested_at THEN
        RAISE EXCEPTION 'a cashback rate change cannot be edited' USING ERRCODE = 'insufficient_privilege';
    END IF;
    RETURN NEW;
END $$;

CREATE TRIGGER cashback_rate_changes_transition BEFORE UPDATE ON cashback_rate_changes
    FOR EACH ROW EXECUTE FUNCTION ifpay_cashback_rate_transition();
CREATE TRIGGER cashback_rate_changes_no_delete BEFORE DELETE ON cashback_rate_changes
    FOR EACH ROW EXECUTE FUNCTION ifpay_forbid_change();

-- ---------------------------------------------------------------------------------------------------------------------
-- Cashback awards: at most one per payment, written with the payment, never changed.
-- ---------------------------------------------------------------------------------------------------------------------

CREATE TABLE cashback_awards (
    id                     uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    payment_request_id     uuid NOT NULL UNIQUE REFERENCES payment_requests (id),
    customer_id            uuid NOT NULL REFERENCES users (id),
    rate_change_id         uuid NOT NULL REFERENCES cashback_rate_changes (id),
    rate_bp                integer NOT NULL CHECK (rate_bp BETWEEN 1 AND 2000),
    amount_minor           bigint NOT NULL CHECK (amount_minor > 0),
    journal_transaction_id uuid NOT NULL UNIQUE REFERENCES journal_transactions (id),
    awarded_at             timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX cashback_awards_customer_day ON cashback_awards (customer_id, awarded_at);

-- Never more than the rate allows on that payment.
CREATE FUNCTION ifpay_check_cashback_award() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    paid bigint;
BEGIN
    SELECT amount_minor INTO paid FROM payment_requests WHERE id = NEW.payment_request_id AND status = 'paid';
    IF paid IS NULL THEN
        RAISE EXCEPTION 'cashback for payment request % that is not paid', NEW.payment_request_id USING ERRCODE = 'check_violation';
    END IF;
    IF NEW.amount_minor * 10000 > paid * NEW.rate_bp THEN
        RAISE EXCEPTION 'cashback % exceeds % bp of %', NEW.amount_minor, NEW.rate_bp, paid USING ERRCODE = 'check_violation';
    END IF;
    RETURN NULL;
END $$;

CREATE CONSTRAINT TRIGGER cashback_awards_within_rate
    AFTER INSERT ON cashback_awards DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION ifpay_check_cashback_award();

-- ---------------------------------------------------------------------------------------------------------------------
-- Refunds: maker/checker, never more than was paid.
-- ---------------------------------------------------------------------------------------------------------------------

CREATE TABLE refunds (
    id                       uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    payment_request_id       uuid NOT NULL REFERENCES payment_requests (id),
    amount_minor             bigint NOT NULL CHECK (amount_minor > 0 AND amount_minor <= 100000),
    reason                   text NOT NULL CHECK (length(reason) BETWEEN 1 AND 200),
    status                   text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'approved', 'rejected')),
    requested_by             uuid NOT NULL REFERENCES users (id),
    requested_at             timestamptz NOT NULL DEFAULT now(),
    decided_by               uuid REFERENCES users (id),
    decided_at               timestamptz,
    decision_note            text NOT NULL DEFAULT '',
    cashback_reversed_minor  bigint NOT NULL DEFAULT 0 CHECK (cashback_reversed_minor >= 0),
    journal_transaction_id   uuid UNIQUE REFERENCES journal_transactions (id),
    CHECK (decided_by IS NULL OR decided_by <> requested_by),
    CHECK ((status = 'pending') = (decided_by IS NULL AND decided_at IS NULL)),
    CHECK ((status = 'approved') = (journal_transaction_id IS NOT NULL)),
    CHECK (cashback_reversed_minor < amount_minor)
);
CREATE INDEX refunds_payment ON refunds (payment_request_id);

CREATE FUNCTION ifpay_refund_transition() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF OLD.status <> 'pending' THEN
        RAISE EXCEPTION 'refund % is already %', OLD.id, OLD.status USING ERRCODE = 'insufficient_privilege';
    END IF;
    IF NEW.status = 'pending' THEN
        RAISE EXCEPTION 'a pending refund can only be approved or rejected' USING ERRCODE = 'insufficient_privilege';
    END IF;
    IF NEW.payment_request_id <> OLD.payment_request_id OR NEW.amount_minor <> OLD.amount_minor
       OR NEW.reason <> OLD.reason OR NEW.requested_by <> OLD.requested_by OR NEW.requested_at <> OLD.requested_at THEN
        RAISE EXCEPTION 'a refund request cannot be edited' USING ERRCODE = 'insufficient_privilege';
    END IF;
    RETURN NEW;
END $$;

CREATE TRIGGER refunds_transition BEFORE UPDATE ON refunds
    FOR EACH ROW EXECUTE FUNCTION ifpay_refund_transition();
CREATE TRIGGER refunds_no_delete BEFORE DELETE ON refunds
    FOR EACH ROW EXECUTE FUNCTION ifpay_forbid_change();

-- The approved refunds of one payment never add up to more than it, and only a paid payment can be refunded.
CREATE FUNCTION ifpay_check_refund_total() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    paid bigint;
    refunded bigint;
BEGIN
    SELECT amount_minor INTO paid FROM payment_requests WHERE id = NEW.payment_request_id AND status = 'paid' FOR UPDATE;
    IF paid IS NULL THEN
        RAISE EXCEPTION 'payment request % is not paid, so it cannot be refunded', NEW.payment_request_id
            USING ERRCODE = 'check_violation';
    END IF;
    SELECT COALESCE(SUM(amount_minor), 0) INTO refunded FROM refunds
     WHERE payment_request_id = NEW.payment_request_id AND status = 'approved';
    IF refunded > paid THEN
        RAISE EXCEPTION 'refunds of payment request % (%) exceed what was paid (%)', NEW.payment_request_id, refunded, paid
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NULL;
END $$;

CREATE CONSTRAINT TRIGGER refunds_within_payment
    AFTER INSERT OR UPDATE ON refunds DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION ifpay_check_refund_total();

DO $$
BEGIN
    EXECUTE 'CREATE TRIGGER cashback_awards_immutable BEFORE UPDATE OR DELETE ON cashback_awards FOR EACH ROW EXECUTE FUNCTION ifpay_forbid_change()';
    EXECUTE 'CREATE TRIGGER cashback_awards_no_truncate BEFORE TRUNCATE ON cashback_awards FOR EACH STATEMENT EXECUTE FUNCTION ifpay_forbid_change()';
END $$;
