-- Payment requests made for another IntraForge application in the same shop (for example a Spare Parts invoice).
--
-- IntraForge brokers the request (internal service-app API): it checks the person may take Pay payments in this shop
-- and names the application that asked. Pay records what the payment is for, shows it to the customer before they
-- confirm, and answers status questions only to the application that made the request. The money side is unchanged:
-- the customer still confirms the exact amount in their own wallet and the journal decides.

ALTER TABLE payment_requests ADD COLUMN reference text CHECK (reference IS NULL OR length(reference) BETWEEN 1 AND 120);
ALTER TABLE payment_requests ADD COLUMN source_app_id uuid;
ALTER TABLE payment_requests ADD COLUMN source_app_name text
    CHECK (source_app_name IS NULL OR length(source_app_name) BETWEEN 1 AND 200);
ALTER TABLE payment_requests ADD COLUMN source_external_id text
    CHECK (source_external_id IS NULL OR length(source_external_id) BETWEEN 1 AND 120);
ALTER TABLE payment_requests ADD CONSTRAINT payment_requests_source_complete
    CHECK ((source_app_id IS NULL) = (source_app_name IS NULL)
       AND (source_app_id IS NULL) = (source_external_id IS NULL));
CREATE INDEX payment_requests_source ON payment_requests (source_app_id, source_external_id)
    WHERE source_app_id IS NOT NULL;

-- What a request is for, and who asked, can never change after it is made (same rule as the amount).
CREATE OR REPLACE FUNCTION ifpay_payment_request_transition() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF OLD.status <> 'pending' THEN
        RAISE EXCEPTION 'payment request % is already %', OLD.id, OLD.status USING ERRCODE = 'insufficient_privilege';
    END IF;
    IF NEW.status = 'pending' THEN
        RAISE EXCEPTION 'a pending payment request can only become paid or cancelled' USING ERRCODE = 'insufficient_privilege';
    END IF;
    IF NEW.merchant_id <> OLD.merchant_id OR NEW.amount_minor <> OLD.amount_minor OR NEW.created_by <> OLD.created_by
       OR NEW.created_at <> OLD.created_at OR NEW.expires_at <> OLD.expires_at
       OR NEW.merchant_verified <> OLD.merchant_verified
       OR NEW.reference IS DISTINCT FROM OLD.reference OR NEW.source_app_id IS DISTINCT FROM OLD.source_app_id
       OR NEW.source_app_name IS DISTINCT FROM OLD.source_app_name
       OR NEW.source_external_id IS DISTINCT FROM OLD.source_external_id THEN
        RAISE EXCEPTION 'a payment request cannot be edited' USING ERRCODE = 'insufficient_privilege';
    END IF;
    IF NEW.status = 'paid' AND OLD.expires_at <= now() THEN
        RAISE EXCEPTION 'payment request % has expired', OLD.id USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END $$;
