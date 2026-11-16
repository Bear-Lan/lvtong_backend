-- 原周界保留 cam_safe ID，新增独立右侧；不覆盖后续人工更改的 IP/账号。
BEGIN;
LOCK TABLE devices IN SHARE ROW EXCLUSIVE MODE;
DO $$
BEGIN
    IF (SELECT COUNT(*) FROM devices WHERE device_id = 'cam_safe') <> 1 THEN
        RAISE EXCEPTION '需要且只能有一条 cam_safe 配置作为周界左';
    END IF;
    IF (SELECT COUNT(*) FROM devices WHERE device_id = 'cam_safe_right') > 1 THEN
        RAISE EXCEPTION 'cam_safe_right 配置重复，请先核对';
    END IF;
END $$;

UPDATE devices
SET device_name = '周界左',
    config = (COALESCE(NULLIF(config, ''), '{}')::jsonb ||
              '{"camera_side":"left","mediamtx_path":"cam3"}'::jsonb)::text,
    updated_time = CURRENT_TIMESTAMP
WHERE device_id = 'cam_safe';

INSERT INTO devices
    (device_id, device_name, device_type, device_model, ip_address, port,
     username, password, config, status)
SELECT 'cam_safe_right', '周界右', device_type, device_model, ip_address, port,
       username, password,
       (COALESCE(NULLIF(config, ''), '{}')::jsonb ||
        '{"role":"safe_camera_right","camera_side":"right","mediamtx_path":"cam7"}'::jsonb)::text,
       0
FROM devices
WHERE device_id = 'cam_safe'
  AND NOT EXISTS (SELECT 1 FROM devices WHERE device_id = 'cam_safe_right');

UPDATE devices SET device_name = '周界右', updated_time = CURRENT_TIMESTAMP
WHERE device_id = 'cam_safe_right' AND device_name IS DISTINCT FROM '周界右';
COMMIT;
