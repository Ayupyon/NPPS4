-- SQLite schema and single/batch receipts captured from the legacy achievement claim guard.
BEGIN TRANSACTION;
CREATE TABLE destiny_achievement_claim (
    user_id INTEGER NOT NULL REFERENCES user(id) ON DELETE CASCADE,
    achievement_id INTEGER NOT NULL,
    period_key TEXT NOT NULL,
    owning_achievement_id INTEGER NOT NULL,
    claimed_at INTEGER NOT NULL,
    request_key TEXT NOT NULL,
    rewards_json TEXT NOT NULL DEFAULT '[]',
    PRIMARY KEY(user_id, achievement_id, period_key)
);
INSERT INTO "destiny_achievement_claim" VALUES(1,100,'permanent',1,1794254400,'body:1794254400:1:batch:0','[{"add_type": 3001, "item_id": 4, "amount": 1, "item_category_id": 4, "reward_box_flag": false, "comment": ""}, {"add_type": 1000, "item_id": 12345, "amount": 2, "item_category_id": 0, "reward_box_flag": false, "comment": ""}, {"add_type": 1001, "item_id": 123, "amount": 1, "item_category_id": 0, "reward_box_flag": true, "comment": ""}]');
INSERT INTO "destiny_achievement_claim" VALUES(1,101,'permanent',2,1794254400,'body:1794254400:2:batch:1','[{"add_type": 3001, "item_id": 4, "amount": 1, "item_category_id": 4, "reward_box_flag": false, "comment": ""}, {"add_type": 1000, "item_id": 12345, "amount": 2, "item_category_id": 0, "reward_box_flag": false, "comment": ""}, {"add_type": 1001, "item_id": 123, "amount": 1, "item_category_id": 0, "reward_box_flag": true, "comment": ""}]');
CREATE TABLE destiny_achievement_claim_migration (
    migration_key TEXT PRIMARY KEY,
    applied_at INTEGER NOT NULL,
    report_json TEXT NOT NULL
);
CREATE TABLE destiny_achievement_claim_receipt (
    user_id INTEGER NOT NULL REFERENCES user(id) ON DELETE CASCADE,
    action TEXT NOT NULL,
    request_key TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    response_json TEXT NOT NULL,
    PRIMARY KEY(user_id, action, request_key)
);
INSERT INTO "destiny_achievement_claim_receipt" VALUES(1,'rewardOpen','body:1794254400:1:batch:0','c8b9f78562a5f76ca075012c792150f9ceb511755d3c0c6a3f777e597ceb952c','{"unit_support_list": [], "before_user_info": {"user_id": 1, "name": "Kemp", "level": 1, "exp": 0, "previous_exp": 0, "next_exp": 6, "game_coin": 0, "sns_coin": 0, "free_sns_coin": 0, "paid_sns_coin": 0, "social_point": 0, "unit_max": 0, "waiting_unit_max": 1000, "current_energy": 0, "energy_max": 25, "energy_full_time": "2026-11-13 12:26:32", "license_live_energy_recoverly_time": 60, "energy_full_need_time": 285992, "over_max_energy": 0, "training_energy": 3, "training_energy_max": 3, "friend_max": 10, "invite_code": "", "insert_date": "2026-11-13 12:26:32", "update_date": "2026-11-13 12:26:32", "tutorial_state": 0, "lp_recovery_item": [], "unlock_random_live_muse": 0, "unlock_random_live_aqours": 0}, "after_user_info": {"user_id": 1, "name": "Kemp", "level": 1, "exp": 0, "previous_exp": 0, "next_exp": 6, "game_coin": 0, "sns_coin": 1, "free_sns_coin": 1, "paid_sns_coin": 0, "social_point": 0, "unit_max": 0, "waiting_unit_max": 1000, "current_energy": 0, "energy_max": 25, "energy_full_time": "2026-11-13 12:26:32", "license_live_energy_recoverly_time": 60, "energy_full_need_time": 285992, "over_max_energy": 0, "training_energy": 3, "training_energy_max": 3, "friend_max": 10, "invite_code": "", "insert_date": "2026-11-13 12:26:32", "update_date": "2026-11-13 12:26:33", "tutorial_state": 0, "lp_recovery_item": [], "unlock_random_live_muse": 0, "unlock_random_live_aqours": 0}, "server_timestamp": 1794540393, "accomplished_achievement_list": [], "unaccomplished_achievement_cnt": 0, "added_achievement_list": [], "new_achievement_cnt": 0, "reward_item_list": [{"add_type": 3001, "item_id": 4, "amount": 1, "item_category_id": 4, "reward_box_flag": false, "comment": ""}, {"add_type": 1000, "item_id": 12345, "amount": 2, "item_category_id": 0, "reward_box_flag": false, "comment": ""}, {"add_type": 1001, "item_id": 123, "amount": 1, "item_category_id": 0, "reward_box_flag": true, "comment": ""}], "present_cnt": 1}');
INSERT INTO "destiny_achievement_claim_receipt" VALUES(1,'rewardOpenAll','body:1794254400:2:batch:1','bcae43cdc85b678e05b0bab3dbdfebc5129cb50e30fea8a4c19cb068d6d98d08','{"unit_support_list": [], "before_user_info": {"user_id": 1, "name": "Kemp", "level": 1, "exp": 0, "previous_exp": 0, "next_exp": 6, "game_coin": 0, "sns_coin": 1, "free_sns_coin": 1, "paid_sns_coin": 0, "social_point": 0, "unit_max": 0, "waiting_unit_max": 1000, "current_energy": 0, "energy_max": 25, "energy_full_time": "2026-11-13 12:26:32", "license_live_energy_recoverly_time": 60, "energy_full_need_time": 285992, "over_max_energy": 0, "training_energy": 3, "training_energy_max": 3, "friend_max": 10, "invite_code": "", "insert_date": "2026-11-13 12:26:32", "update_date": "2026-11-13 12:26:33", "tutorial_state": 0, "lp_recovery_item": [], "unlock_random_live_muse": 0, "unlock_random_live_aqours": 0}, "after_user_info": {"user_id": 1, "name": "Kemp", "level": 1, "exp": 0, "previous_exp": 0, "next_exp": 6, "game_coin": 0, "sns_coin": 2, "free_sns_coin": 2, "paid_sns_coin": 0, "social_point": 0, "unit_max": 0, "waiting_unit_max": 1000, "current_energy": 0, "energy_max": 25, "energy_full_time": "2026-11-13 12:26:32", "license_live_energy_recoverly_time": 60, "energy_full_need_time": 285992, "over_max_energy": 0, "training_energy": 3, "training_energy_max": 3, "friend_max": 10, "invite_code": "", "insert_date": "2026-11-13 12:26:32", "update_date": "2026-11-13 12:26:33", "tutorial_state": 0, "lp_recovery_item": [], "unlock_random_live_muse": 0, "unlock_random_live_aqours": 0}, "server_timestamp": 1794540393, "accomplished_achievement_list": [], "unaccomplished_achievement_cnt": 0, "added_achievement_list": [], "new_achievement_cnt": 0, "reward_item_list": [{"add_type": 3001, "item_id": 4, "amount": 1, "item_category_id": 4, "reward_box_flag": false, "comment": ""}, {"add_type": 1000, "item_id": 12345, "amount": 2, "item_category_id": 0, "reward_box_flag": false, "comment": ""}, {"add_type": 1001, "item_id": 123, "amount": 1, "item_category_id": 0, "reward_box_flag": true, "comment": ""}], "present_cnt": 2, "is_last": true, "opened_num": 1}');
INSERT INTO "destiny_achievement_claim_receipt" VALUES(1,'rewardOpenAll','body:1794254400:2:batch:2','bcae43cdc85b678e05b0bab3dbdfebc5129cb50e30fea8a4c19cb068d6d98d08','{"unit_support_list": [], "before_user_info": {"user_id": 1, "name": "Kemp", "level": 1, "exp": 0, "previous_exp": 0, "next_exp": 6, "game_coin": 0, "sns_coin": 2, "free_sns_coin": 2, "paid_sns_coin": 0, "social_point": 0, "unit_max": 0, "waiting_unit_max": 1000, "current_energy": 0, "energy_max": 25, "energy_full_time": "2026-11-13 12:26:32", "license_live_energy_recoverly_time": 60, "energy_full_need_time": 285992, "over_max_energy": 0, "training_energy": 3, "training_energy_max": 3, "friend_max": 10, "invite_code": "", "insert_date": "2026-11-13 12:26:32", "update_date": "2026-11-13 12:26:33", "tutorial_state": 0, "lp_recovery_item": [], "unlock_random_live_muse": 0, "unlock_random_live_aqours": 0}, "after_user_info": {"user_id": 1, "name": "Kemp", "level": 1, "exp": 0, "previous_exp": 0, "next_exp": 6, "game_coin": 0, "sns_coin": 2, "free_sns_coin": 2, "paid_sns_coin": 0, "social_point": 0, "unit_max": 0, "waiting_unit_max": 1000, "current_energy": 0, "energy_max": 25, "energy_full_time": "2026-11-13 12:26:32", "license_live_energy_recoverly_time": 60, "energy_full_need_time": 285992, "over_max_energy": 0, "training_energy": 3, "training_energy_max": 3, "friend_max": 10, "invite_code": "", "insert_date": "2026-11-13 12:26:32", "update_date": "2026-11-13 12:26:33", "tutorial_state": 0, "lp_recovery_item": [], "unlock_random_live_muse": 0, "unlock_random_live_aqours": 0}, "server_timestamp": 1794540393, "accomplished_achievement_list": [], "unaccomplished_achievement_cnt": 0, "added_achievement_list": [], "new_achievement_cnt": 0, "reward_item_list": [], "present_cnt": 2, "is_last": true, "opened_num": 0}');
CREATE TABLE destiny_achievement_claim_review (
    user_id INTEGER NOT NULL REFERENCES user(id) ON DELETE CASCADE,
    achievement_id INTEGER NOT NULL,
    blocked_period TEXT NOT NULL,
    owning_achievement_id INTEGER NOT NULL,
    reason TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    resolved_at INTEGER,
    PRIMARY KEY(user_id, achievement_id, blocked_period)
);
CREATE TABLE destiny_achievement_claim_review_decision (
    user_id INTEGER NOT NULL REFERENCES user(id) ON DELETE CASCADE,
    achievement_id INTEGER NOT NULL,
    blocked_period TEXT NOT NULL,
    plan_hash TEXT NOT NULL,
    decision TEXT NOT NULL CHECK(decision IN ('confirm_paid','allow_retry')),
    approved_by TEXT NOT NULL,
    evidence TEXT NOT NULL,
    plan_json TEXT NOT NULL,
    applied_at INTEGER NOT NULL,
    PRIMARY KEY(user_id, achievement_id, blocked_period)
);
COMMIT;
