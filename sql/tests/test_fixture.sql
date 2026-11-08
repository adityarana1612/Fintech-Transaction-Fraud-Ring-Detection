/* =============================================================================
   tests/test_fixture.sql  -  HAND-MADE TEST FIXTURE (NOT analytical data)
   -----------------------------------------------------------------------------
   Loaded into a SEPARATE database (fraud_test) by scripts/06_validate.py to prove
   the engine logic on cases with known answers. These rows never touch the
   analytical database and are not part of any finding.

   Scenarios and expected engine results
   S1 money-flow chain with a cycle and a late hop
      A->B 10:00, B->C 11:00, C->D 12:00, D->A 13:00 (closes cycle), D->E next day 14:00
      expected maximal chains: [A,B,C,D] (A->B->C->D; D->A is blocked because A is
      already on the path) and [B,C,D,A] (anchored at B->C, 24 h window starts there).
      D->E is outside every 24 h window -> appears in no chain.
   S2 velocity burst: account V sends 7 txns within 8 minutes, then 1 txn 40 min later
      expected: exactly one multi-txn episode for V with txn_count = 7 (each txn counted
      once), max_txns_any_10m = 7, breaches_spec_rule = TRUE; V is the only breaching account.
   S3 shared device: X and Y use D9999001 18 minutes apart (pair), Z uses it 3 h later (no pair)
   S4 shared IP: X and W use 41.58.250.250 4 minutes apart (pair); W uses it again 20 min later (no pair)
   S5 filler rows so percentiles / tiers have a population
   ============================================================================= */
SET search_path TO fraud, public;

INSERT INTO stg_transactions (transaction_id, txn_ts, sender_account, receiver_account, transaction_type, merchant_category,
    merchant_group, location, device_channel, payment_channel, device_id, ip_address, amount_ngn, is_fraud, fraud_type,
    src_new_device_flag, bvn_linked, sender_persona, src_time_since_last_txn, src_spending_deviation_score,
    src_velocity_score, src_geo_anomaly_score, txn_date, txn_hour, is_self_transfer) VALUES
-- S1 chain:   A=1000000001 B=1000000002 C=1000000003 D=1000000004 E=1000000005
('T9000001','2023-03-01 10:00:00','1000000001','1000000002','TRANSFER','Paystack Payment','Payment Gateway','Lagos','MOBILE','MOBILE_APP','D9000001','197.210.1.1','500000.00','1','Account Takeover','1','1','Trader','','0','1','0.1','2023-03-01','10','0'),
('T9000002','2023-03-01 11:00:00','1000000002','1000000003','TRANSFER','Paystack Payment','Payment Gateway','Lagos','MOBILE','MOBILE_APP','D9000002','197.210.1.2','490000.00','0','','1','1','Trader','','0','1','0.1','2023-03-01','11','0'),
('T9000003','2023-03-01 12:00:00','1000000003','1000000004','TRANSFER','Paystack Payment','Payment Gateway','Lagos','MOBILE','MOBILE_APP','D9000003','197.210.1.3','480000.00','0','','1','1','Trader','','0','1','0.1','2023-03-01','12','0'),
('T9000004','2023-03-01 13:00:00','1000000004','1000000001','TRANSFER','Paystack Payment','Payment Gateway','Lagos','MOBILE','MOBILE_APP','D9000004','197.210.1.4','470000.00','0','','1','1','Trader','','0','1','0.1','2023-03-01','13','0'),
('T9000005','2023-03-02 14:00:00','1000000004','1000000005','TRANSFER','Paystack Payment','Payment Gateway','Lagos','MOBILE','MOBILE_APP','D9000005','197.210.1.5','460000.00','0','','1','1','Trader','','0','1','0.1','2023-03-02','14','0'),
-- S2 velocity: V=1000000010 pays receivers 1000000101..108
('T9000011','2023-04-01 09:00:00','1000000010','1000000101','PAYMENT','Bet9ja Stake','Betting & Gaming','Kano','WEB','CARD','D9000011','102.89.1.1','1000.00','0','','1','1','Student','','0','1','0.1','2023-04-01','9','0'),
('T9000012','2023-04-01 09:01:00','1000000010','1000000102','PAYMENT','Bet9ja Stake','Betting & Gaming','Kano','WEB','CARD','D9000012','102.89.1.1','1000.00','0','','1','1','Student','','0','1','0.1','2023-04-01','9','0'),
('T9000013','2023-04-01 09:02:00','1000000010','1000000103','PAYMENT','Bet9ja Stake','Betting & Gaming','Kano','WEB','CARD','D9000013','102.89.1.1','1000.00','0','','1','1','Student','','0','1','0.1','2023-04-01','9','0'),
('T9000014','2023-04-01 09:04:00','1000000010','1000000104','PAYMENT','Bet9ja Stake','Betting & Gaming','Kano','WEB','CARD','D9000014','102.89.1.1','1000.00','0','','1','1','Student','','0','1','0.1','2023-04-01','9','0'),
('T9000015','2023-04-01 09:05:00','1000000010','1000000105','PAYMENT','Bet9ja Stake','Betting & Gaming','Kano','WEB','CARD','D9000015','102.89.1.1','1000.00','0','','1','1','Student','','0','1','0.1','2023-04-01','9','0'),
('T9000016','2023-04-01 09:07:00','1000000010','1000000106','PAYMENT','Bet9ja Stake','Betting & Gaming','Kano','WEB','CARD','D9000016','102.89.1.1','1000.00','0','','1','1','Student','','0','1','0.1','2023-04-01','9','0'),
('T9000017','2023-04-01 09:08:00','1000000010','1000000107','PAYMENT','Bet9ja Stake','Betting & Gaming','Kano','WEB','CARD','D9000017','102.89.1.1','1000.00','0','','1','1','Student','','0','1','0.1','2023-04-01','9','0'),
('T9000018','2023-04-01 09:48:00','1000000010','1000000108','PAYMENT','Bet9ja Stake','Betting & Gaming','Kano','WEB','CARD','D9000018','102.89.1.1','1000.00','0','','1','1','Student','','0','1','0.1','2023-04-01','9','0'),
-- S3 shared device D9999001: X=1000000020 Y=1000000021 Z=1000000022 ; S4 shared IP 41.58.250.250: X and W=1000000023
('T9000021','2023-05-01 08:00:00','1000000020','1000000201','PAYMENT','Jumia Purchase','E-commerce','Abuja','MOBILE','CARD','D9999001','41.58.250.250','20000.00','0','','1','1','Trader','','0','1','0.1','2023-05-01','8','0'),
('T9000022','2023-05-01 08:18:00','1000000021','1000000202','PAYMENT','Jumia Purchase','E-commerce','Abuja','MOBILE','CARD','D9999001','197.210.9.9','20000.00','0','','1','1','Trader','','0','1','0.1','2023-05-01','8','0'),
('T9000023','2023-05-01 11:30:00','1000000022','1000000203','PAYMENT','Jumia Purchase','E-commerce','Abuja','MOBILE','CARD','D9999001','197.210.9.10','20000.00','0','','1','1','Trader','','0','1','0.1','2023-05-01','11','0'),
('T9000024','2023-05-01 08:04:00','1000000023','1000000204','PAYMENT','Jumia Purchase','E-commerce','Abuja','WEB','CARD','D9000024','41.58.250.250','20000.00','0','','0','1','Trader','','0','1','0.1','2023-05-01','8','0'),
('T9000025','2023-05-01 08:24:00','1000000023','1000000205','PAYMENT','Jumia Purchase','E-commerce','Abuja','WEB','CARD','D9000025','41.58.250.250','20000.00','0','','0','1','Trader','','0','1','0.1','2023-05-01','8','0'),
-- S5 filler
('T9000031','2023-06-01 12:00:00','1000000030','1000000301','DEPOSIT','Shoprite Purchase','Retail & Groceries','Enugu','POS','CARD','D9000031','105.112.1.1','15000.00','0','','0','1','Salary Earner','','0','1','0.1','2023-06-01','12','0'),
('T9000032','2023-06-02 12:00:00','1000000031','1000000302','WITHDRAWAL','ATM Withdrawal','Cash Access','Enugu','ATM','CARD','D9000032','105.112.1.2','25000.00','0','','0','1','Salary Earner','','0','1','0.1','2023-06-02','12','0'),
('T9000033','2023-06-03 12:00:00','1000000032','1000000303','PAYMENT','DSTV Payment','Bills & Utilities','Enugu','WEB','CARD','D9000033','105.112.1.3','12000.00','0','','0','1','Salary Earner','','0','1','0.1','2023-06-03','12','0'),
('T9000034','2023-06-04 12:00:00','1000000033','1000000304','PAYMENT','DSTV Payment','Bills & Utilities','Enugu','WEB','CARD','D9000034','105.112.1.4','13000.00','1','Identity Fraud','1','1','Salary Earner','','0','1','0.1','2023-06-04','12','0');
