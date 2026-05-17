from sync_service.extractor import extract_form_metadata, bool_val

form = extract_form_metadata('hcaspay', 'empmu')
for dc in form.get('datacontainers', []):
    for field in dc.get('fields', []):
        fname = field.get('name', '')
        if fname.lower() in ('designame', 'deptname'):
            mode   = (field.get('modeofentry') or '').lower()
            src_tb = (field.get('source_table') or '').lower()
            src_f  = field.get('sourcefield') or ''
            print(f"field={fname} mode={mode} src_tb='{src_tb}' src_f='{src_f}'")