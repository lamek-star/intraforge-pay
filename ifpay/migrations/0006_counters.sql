-- More than one counter in a shop: each payment request says at which counter it was taken, so each cashier sees
-- their own counter's day and the shop sees every counter's total. A request made by another IntraForge application,
-- or by a cashier who has not chosen a counter, has none.

CREATE TABLE counters (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    merchant_id uuid NOT NULL REFERENCES merchants (id),
    name        text NOT NULL CHECK (length(btrim(name)) BETWEEN 1 AND 40),
    active      boolean NOT NULL DEFAULT true,
    created_at  timestamptz NOT NULL DEFAULT now(),
    UNIQUE (merchant_id, name)
);

ALTER TABLE payment_requests ADD COLUMN counter_id uuid REFERENCES counters (id);

-- A counter belongs to the request's own shop (checked by the database, not only the code).
CREATE FUNCTION ifpay_payment_request_counter() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.counter_id IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM counters WHERE id = NEW.counter_id AND merchant_id = NEW.merchant_id
    ) THEN
        RAISE EXCEPTION 'counter % is not one of this shop''s counters', NEW.counter_id USING ERRCODE = 'check_violation';
    END IF;
    IF TG_OP = 'UPDATE' AND NEW.counter_id IS DISTINCT FROM OLD.counter_id THEN
        RAISE EXCEPTION 'a payment request''s counter cannot change' USING ERRCODE = 'insufficient_privilege';
    END IF;
    RETURN NEW;
END $$;

CREATE TRIGGER payment_requests_counter BEFORE INSERT OR UPDATE OF counter_id ON payment_requests
    FOR EACH ROW EXECUTE FUNCTION ifpay_payment_request_counter();
