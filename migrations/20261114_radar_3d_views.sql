-- 新增同一帧点云的顶部/侧面图片；历史记录留空，不删除原字段或记录。
BEGIN;
ALTER TABLE vehicle_inspections ADD COLUMN IF NOT EXISTS radar_3d_top_image_path TEXT NOT NULL DEFAULT '';
ALTER TABLE vehicle_inspections ADD COLUMN IF NOT EXISTS radar_3d_side_image_path TEXT NOT NULL DEFAULT '';
COMMENT ON COLUMN vehicle_inspections.radar_3d_top_image_path IS '与3D点云同帧的顶部XY正交投影图';
COMMENT ON COLUMN vehicle_inspections.radar_3d_side_image_path IS '与3D点云同帧的侧面XZ正交投影图';
COMMIT;
