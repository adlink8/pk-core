"""Phase 14 分批 backfill 循环。每次处理 100 条，重复直到全部完成。"""

from personal_knowledge.core.sqlite import connect_rw

from personal_knowledge.core.project_paths import UNIFIED_DB
from personal_knowledge.application.knowledge.build_knowledge_units_prod import resume_run, process_run

run_id = '731a6a8a0994ae9a5ae94a117b58dd1e'
model = 'gemini-3.5-flash-lite'

for batch in range(1, 50):
    resume_run(run_id, model)
    stats = process_run(run_id, model, max_items=100)
    
    con = connect_rw(UNIFIED_DB)
    pend = con.execute(
        "SELECT COUNT(*) FROM knowledge_run_items WHERE run_id=? AND status IN ('pending','retryable')",
        (run_id,)
    ).fetchone()[0]
    succ = con.execute(
        "SELECT COUNT(*) FROM knowledge_run_items WHERE run_id=? AND status='succeeded'",
        (run_id,)
    ).fetchone()[0]
    units = con.execute(
        "SELECT COUNT(*) FROM knowledge_units WHERE run_id=?", (run_id,)
    ).fetchone()[0]
    con.close()
    
    print(f"batch {batch:2d}: processed={stats['processed']:3d} succeeded={stats['succeeded']:3d} failed={stats['failed']:3d} | total_succ={succ} units={units} | remaining={pend}")
    
    if pend == 0:
        print("ALL DONE")
        break
