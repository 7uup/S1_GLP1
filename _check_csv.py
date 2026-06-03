import asyncio,sys,io,csv;sys.stdout=io.TextIOWrapper(sys.stdout.buffer,encoding='utf-8')
from app.config import load_config
from app.glpi_client import GLPIClient

async def main():
    cfg=load_config()
    # Read CSVs
    csv_rows=[]
    for f in ['Sentinels_Full_Report_20260602（2002控制端）.csv','Sentinels_Full_Report_20260602（euce1控制台）.csv']:
        with open(f,'r',encoding='utf-8-sig') as ff:
            for row in csv.DictReader(ff):
                sn=(row.get('Serial Number') or '').strip()
                uid=(row.get('Agent UUID') or '').strip()
                cust=(row.get('Customer Identifier') or '').strip()
                ep=row.get('Endpoint Name','')
                console='HK' if '2002' in f else 'SZ'
                csv_rows.append({'serial':sn,'uuid':uid,'customer':cust,'endpoint':ep,'console':console})
    print('CSV total: %d'%len(csv_rows))

    # GLPI
    glpi=GLPIClient(cfg.glpi);await glpi.init()
    r=await glpi._client.get('/Computer',params={'expand_dropdowns':'false','range':'0-200'},headers=glpi._headers())
    await glpi.close()
    glpi_rows=r.json()
    print('GLPI total: %d\n'%len(glpi_rows))

    # Build GLPI indexes
    glpi_serial={}
    glpi_uuid={}
    for comp in glpi_rows:
        sn=(comp.get('serial') or '').strip()
        uid=(comp.get('uuid') or '').strip()
        if sn: glpi_serial[sn]=comp
        if uid: glpi_uuid[uid]=comp

    # Compare
    both_match=0;serial_only=0;uuid_only=0;neither=0
    only_by_serial=[];only_by_uuid=[];no_match=[]

    for row in csv_rows:
        sn=row['serial'];uid=row['uuid']
        by_serial=sn in glpi_serial
        by_uuid=uid in glpi_uuid
        if by_serial and by_uuid:
            both_match+=1
        elif by_serial and not by_uuid:
            serial_only+=1;only_by_serial.append(row)
        elif not by_serial and by_uuid:
            uuid_only+=1;only_by_uuid.append(row)
        else:
            neither+=1;no_match.append(row)

    print('Both serial and uuid match: %d'%both_match)
    print('Serial match only (uuid no): %d'%serial_only)
    print('UUID match only (serial no): %d'%uuid_only)
    print('Neither match: %d\n'%neither)

    if only_by_serial:
        print('--- Serial match / UUID NOT match ---')
        for r in only_by_serial[:10]:
            glpi_c=glpi_serial[r['serial']]
            g_uid=(glpi_c.get('uuid') or '')[:16]
            print('[%s] %-30s CSV_uuid=%s  GLPI_uuid=%s'%(r['console'],r['endpoint'][:30],r['uuid'][:16],g_uid))
        if len(only_by_serial)>10:print('  ... %d more'%(len(only_by_serial)-10))

    if only_by_uuid:
        print('\n--- UUID match / Serial NOT match ---')
        for r in only_by_uuid[:10]:
            glpi_c=glpi_uuid[r['uuid']]
            g_sn=(glpi_c.get('serial') or '')
            print('[%s] %-30s CSV_serial=%-18s GLPI_serial=%s'%(r['console'],r['endpoint'][:30],r['serial'],g_sn))
        if len(only_by_uuid)>10:print('  ... %d more'%(len(only_by_uuid)-10))

    if no_match:
        print('\n--- Neither match (S1 has, GLPI lacks) ---')
        for r in no_match[:10]:
            print('[%s] %-30s serial=%s  uuid=%s  customer=%s'%(r['console'],r['endpoint'][:30],r['serial'],r['uuid'][:16],r['customer'][:10]))
        print('  ... total %d'%len(no_match))

asyncio.run(main())
