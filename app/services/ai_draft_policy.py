"""AI仅补空或更新同源AI值。人工编辑是会话内不可逆的优先标记。"""
MANUAL_FIELDS = {'plate', 'truckType', 'containerType', 'loadRate'}
GROUP_FIELDS = {'truck': {'truckType', 'containerType'}, 'license': {'plate'}, 'load': {'loadRate'}}


def ai_change_allowed(doc, change, incoming_context):
    group, path = change.get('aiGroup'), change['path']
    if len(path) != 2:
        return False
    base = change.get('aiBaseRevision')
    if group not in GROUP_FIELDS or type(base) is not int or not 0 <= base <= doc['revision']:
        return False
    root, field = path
    supported = ((root == 'form' and field in GROUP_FIELDS[group])
                 or (root == 'aiMarks' and field in GROUP_FIELDS[group] | ({'license', 'licenseGc', 'weightRange'} if group == 'license' else set()))
                 or (root == 'aiContext' and field == group)
                 or (group == 'license' and (root == 'licensePaths' or (root == 'captureThumbs' and field == 'license'))))
    if not supported:
        return False
    draft = doc['draft']
    context = incoming_context.get(group) or draft.get('aiContext', {}).get(group, {})
    if context:
        if group == 'truck' and context.get('source') != draft.get('panelImages', {}).get('body', {}).get('1', ''):
            return False
        if group == 'load' and context.get('source') not in [draft.get('panelImages', {}).get('transparent', {}).get(k, '') for k in ('1', '3')]:
            return False
        if group == 'load' and context.get('container') != draft.get('form', {}).get('containerType', ''):
            return False
        if group == 'license':
            for key, before, after in (('license', 'inputSource', 'source'), ('licenseGc', 'inputHangSource', 'hangSource')):
                current = draft.get('licensePaths', {}).get(key, '')
                if current not in (context.get(before, ''), context.get(after, '')):
                    return False
    # 没有来源上下文的旧客户端仍须遵守原版本保护。
    else:
        sources = {'truck': ['panelImages.body'], 'license': ['licensePaths.license', 'licensePaths.licenseGc'], 'load': ['panelImages.transparent', 'form.containerType']}
        if any(doc['_versions'].get(k, 0) > base for k in sources[group]):
            return False
    if root in ('form', 'aiMarks') and field in MANUAL_FIELDS:
        if draft.get('manualFields', {}).get(field):
            return False
        value = draft.get('form', {}).get(field, '')
        if value not in ('', '--', None) and draft.get('aiMarks', {}).get(field) != value:
            return False
        if doc['_versions'].get('form.' + field, 0) > base and value != change['value']:
            return False
    return True
