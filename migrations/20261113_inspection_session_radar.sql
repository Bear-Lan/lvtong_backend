-- 本机开发库迁移；现场测试期间不执行。
BEGIN;
CREATE TABLE IF NOT EXISTS inspection_session (id INTEGER PRIMARY KEY, payload TEXT NOT NULL);
ALTER TABLE vehicle_inspections ADD COLUMN IF NOT EXISTS inspection_session_id TEXT UNIQUE;
ALTER TABLE vehicle_inspections ADD COLUMN IF NOT EXISTS appointment_radar_image_path TEXT NOT NULL DEFAULT '';
ALTER TABLE vehicle_inspections ADD COLUMN IF NOT EXISTS radar_3d_image_path TEXT NOT NULL DEFAULT '';
ALTER TABLE vehicle_inspections DROP COLUMN IF EXISTS radar_side_image_path;
ALTER TABLE vehicle_inspections DROP COLUMN IF EXISTS radar_top_image_path;
ALTER TABLE vehicle_inspections DROP COLUMN IF EXISTS radar_head_image_path;
COMMENT ON COLUMN vehicle_inspections.appointment_radar_image_path IS '预约界面预约雷达图';
COMMENT ON COLUMN vehicle_inspections.radar_3d_image_path IS '点云界面3D视图截图';
COMMIT;
