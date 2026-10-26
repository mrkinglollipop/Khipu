import io, os, pty, sys
from contextlib import redirect_stdout
import pytest
from khipu import cli, routing, owner_direction, owner_turns, owner_act, owed_tidy, owed_origin

# Capture before origin_db installs its explicit synthetic evidence seam.
REAL_OWNERSHIP_POLICY = getattr(owed_origin, "_ownership_established", lambda row: True)
from tests.hermetic_pg import hub, pg_cluster
from tests.test_owed_origin_correction import origin_db, seed, command, allow_app
from tests.test_owed_origin_auth_regressions import owner_prompt, REAL_TYPED_YES


def pty_cli(monkeypatch, argv):
    monkeypatch.setattr(routing, 'typed_yes', REAL_TYPED_YES)
    master, slave = pty.openpty()
    try:
        os.write(master, b'yes\n')
        with os.fdopen(slave, 'r') as terminal:
            with monkeypatch.context() as mp:
                mp.setattr(sys, 'stdin', terminal)
                output = io.StringIO()
                with redirect_stdout(output):
                    args = cli.build_parser().parse_args(argv)
                    rc = args.func(args)
                return rc, output.getvalue()
    finally:
        os.close(master)


def test_job_cannot_enable_its_own_direction_through_pty(origin_db, owner_prompt, monkeypatch):
    cid = seed(origin_db)
    _, pending = command('--request')
    owner_prompt.say('Synthetic unattended job instructions only', entry='sdk-cli')
    assert command('--confirm', pending['request'], '--owner-direction')[0] != 0
    rc, output = pty_cli(monkeypatch, ['owner-direction', 'set', 'trust', 'claude_code:sdk-cli'])
    assert rc != 0, output
    ok, out = owner_act.act('owed.correct_job_origin.confirm', {'request': pending['request']})
    with origin_db.cursor() as cur:
        cur.execute('SELECT owner,origin FROM commitments WHERE id=%s', (cid,))
        state = cur.fetchone()
    print('JOB_TRUST_RESULT', ok, out.get('changed'), state)
    assert not ok and state == ('user', 'conversation'), (ok, out, state)


def test_stale_session_cannot_extend_window_through_pty(origin_db, owner_prompt, monkeypatch):
    cid = seed(origin_db)
    _, pending = command('--request')
    owner_prompt.say(ago=7*3600)
    assert command('--confirm', pending['request'], '--owner-direction')[1]['code'] == 'stale'
    rc, output = pty_cli(monkeypatch, ['owner-direction', 'set', 'latest_window_h', '24'])
    assert rc != 0, output
    rc, result = command('--confirm', pending['request'], '--owner-direction')
    print('STALE_WINDOW_RESULT', rc, result.get('changed'))
    assert rc != 0, result


def test_correction_undo_cannot_use_fake_terminal(origin_db, monkeypatch):
    cid = seed(origin_db)
    _, pending = command('--request')
    allow_app(monkeypatch)
    _, applied = command('--confirm', pending['request'], '--from-app')
    monkeypatch.setattr(routing, '_caller', lambda: {'app':False,'chain':['job']})
    rc, output = pty_cli(monkeypatch, ['owed', 'tidy', '--project', 'example/widget', '--undo', applied['batches'][0]])
    print('PTY_UNDO_RESULT', rc, output)
    assert rc != 0, output


def test_migration_timestamp_does_not_prove_writer_metadata(hub, monkeypatch, tmp_path):
    from khipu import commitments, db
    from tests.hermetic_pg import chain_dir
    from tests.fact_seed import adopt
    monkeypatch.setenv('KHIPU_DATA_DIR', str(tmp_path))
    monkeypatch.setenv('KHIPU_USER_ALIASES', 'owner')
    conn = hub.connect()
    hub.migrate(directory=chain_dir(tmp_path, '0033'), conn=conn)
    conn.commit()
    db._TABLE_COLUMNS_CACHE.clear()
    with conn.cursor() as cur:
        assert 'owner_reassigned_at' not in db.table_columns(cur, 'commitments')
    conn.commit()
    # A long-lived writer retains its cached pre-migration column list.
    hub.migrate(directory=chain_dir(tmp_path, '0035'), conn=conn)
    conn.commit()
    with conn.transaction(), conn.cursor() as cur:
        cur.execute("INSERT INTO episodes(ts,session_id,parent_session_id,summary,raw) VALUES(now(),'cached-writer-job','parent','Synthetic','{}') RETURNING id")
        eid = cur.fetchone()[0]
        cur.execute("INSERT INTO commitments(project,text,owner,kind,opened_episode,opened_at,content_hash) VALUES('example/widget','Should the synthetic fixture use a queue?','assistant','question',%s,clock_timestamp(),'cached-writer') RETURNING id",(eid,))
        cid = cur.fetchone()[0]
        assert commitments.reassign(cur,cid,'user',by='app',reason='I own this')
        cur.execute('SELECT owner_reassigned_at FROM commitments WHERE id=%s',(cid,))
        assert cur.fetchone()[0] is None
    hub.migrate(conn=conn)
    conn.commit()
    db._TABLE_COLUMNS_CACHE.clear()
    with conn.transaction(), conn.cursor() as cur:
        adopt(cur,'commitment',cid)
        cur.execute('UPDATE commitments SET origin_reviewed_at=NULL WHERE id=%s',(cid,))
    monkeypatch.setattr(db,'connect',lambda **kw:hub.connect())
    candidates=command()[1]['items']
    print('CACHED_WRITER_CANDIDATES',candidates)
    assert cid not in [x['id'] for x in candidates], candidates


def test_lost_commit_reply_retains_owner_action_undo(origin_db, hub, monkeypatch):
    from khipu import db
    cid = seed(origin_db)
    _, pending = command('--request')
    allow_app(monkeypatch)
    original_connect = db.connect
    class LostReply:
        def __init__(self):
            self.conn=hub.connect()
        def __enter__(self):
            return self.conn.__enter__()
        def __exit__(self,*args):
            self.conn.__exit__(*args)
            if args[0] is None:
                raise db.psycopg.OperationalError('synthetic lost COMMIT reply after durable commit')
    monkeypatch.setattr(db,'connect',lambda **kw:LostReply())
    with pytest.raises(db.psycopg.OperationalError,match='lost COMMIT'):
        command('--confirm',pending['request'],'--from-app')
    monkeypatch.setattr(db,'connect',original_connect)
    with origin_db.cursor() as cur:
        cur.execute('SELECT owner,origin FROM commitments WHERE id=%s',(cid,))
        assert cur.fetchone()==('agent','job')
        cur.execute('SELECT state FROM commitment_origin_requests WHERE id=%s',(pending['request'],))
        assert cur.fetchone()[0]=='applied'
    line=next(a for a in owner_direction.actions() if a['action']=='owed.correct_job_origin.confirm')
    refusal=owner_direction._undo_refusal(line,line['id'])
    rc,retry=command('--confirm',pending['request'],'--from-app')
    print('LOST_COMMIT',line['state'],refusal,'RETRY',retry)
    assert refusal is None, (line['state'],refusal,retry)
    assert retry['batches'] == line['before']['batches']
    assert retry['changed'] == 0 and retry['already_applied']
    assert owner_direction.undo(line['id'], 'app')['restored'] == 1
    with origin_db.cursor() as cur:
        cur.execute('SELECT owner,origin FROM commitments WHERE id=%s', (cid,))
        assert cur.fetchone() == ('user','conversation')


@pytest.mark.parametrize('entry', sorted(owner_turns.MACHINE_ENTRYPOINTS))
def test_configured_machine_trust_never_authorizes(origin_db, owner_prompt, entry):
    cid = seed(origin_db)
    _, pending = command('--request')
    owner_turns.write_settings({'trusted': [*owner_turns.settings()['trusted'], 'claude_code:' + entry]})
    owner_prompt.say(entry=entry)
    ok, out = owner_act.act('owed.correct_job_origin.confirm', {'request': pending['request']})
    assert not ok, out
    with origin_db.cursor() as cur:
        cur.execute('SELECT owner,origin FROM commitments WHERE id=%s', (cid,))
        assert cur.fetchone() == ('user','conversation')


def test_fake_terminal_cannot_undo_authorization_settings(origin_db, monkeypatch):
    from khipu import cli
    allow_app(monkeypatch)
    args = cli.build_parser().parse_args(['owner-direction','set','latest_window_h','1','--from-app'])
    with redirect_stdout(io.StringIO()):
        assert args.func(args) == 0
    line = next(a for a in owner_direction.actions() if a['action'] == 'direction.set')
    monkeypatch.setattr(routing, '_caller', lambda: {'app':False,'chain':['job']})
    rc, out = pty_cli(monkeypatch, ['owner-direction','undo',line['id']])
    assert rc != 0, out
    assert owner_turns.settings()['latest_window_h'] == 1


def test_historical_row_without_positive_ownership_proof_is_quarantined(origin_db, monkeypatch):
    from khipu import owed_origin
    cid = seed(origin_db)
    _, pending = command('--request')
    # Restore the real evidence policy after seed's synthetic test-only proof.
    monkeypatch.setattr(owed_origin, '_ownership_established', REAL_OWNERSHIP_POLICY, raising=False)
    assert cid not in [r['id'] for r in command()[1]['items']]
    allow_app(monkeypatch)
    result = command('--confirm',pending['request'],'--from-app')[1]
    assert result['changed'] == 0 and result['skipped'] == [cid]


@pytest.mark.parametrize('gate', ['cli','terminal',None])
def test_low_level_correction_undo_requires_strong_gate(origin_db, monkeypatch, gate):
    cid = seed(origin_db)
    _, pending = command('--request')
    allow_app(monkeypatch)
    _, applied = command('--confirm',pending['request'],'--from-app')
    monkeypatch.setattr(routing, '_caller', lambda: {'app':False,'chain':['job']})
    monkeypatch.setattr(routing, 'typed_yes', lambda prompt: True)
    out = owed_tidy.undo(applied['batches'][0], owner_gate=gate)
    assert not out['ok'] and out['code'] == 'owner_confirmation_required', out
