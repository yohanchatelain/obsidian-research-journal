import json
from pathlib import Path

import pytest

from research_journal.sync import exclusive, filters, run, settings


@pytest.fixture
def sync_settings(journal, tmp_path):
    (journal.config.vault / 'RCLONE_TEST').write_text('access marker\n')
    path = tmp_path / 'sync.json'
    path.write_text(json.dumps({'schema_version': 1, 'rclone': '/usr/bin/rclone', 'hosts': [
        {'name': 'cluster', 'path': '/home/user/Obsidian Vault', 'backup': '/home/user/journal-backups'}]}))
    return path


def test_explicit_baseline_and_no_automatic_resync(journal, sync_settings, monkeypatch):
    calls = []
    def execute(argv, timeout):
        calls.append(argv)
        return {'returncode': 0, 'stdout': '', 'stderr': ''}
    monkeypatch.setattr('research_journal.sync.execute', execute)
    assert not run(journal.config, sync_settings)['ok']
    assert not calls
    assert run(journal.config, sync_settings, initialize=True)['ok']
    assert '--resync' in calls[0] and '--check-access' in calls[0]
    assert '--backup-dir1' in calls[0] and '--backup-dir2' in calls[0]
    assert '--inplace' not in calls[0] and '--force' not in calls[0]
    assert run(journal.config, sync_settings)['ok']
    assert '--resync' not in calls[-1]
    assert not run(journal.config, sync_settings, initialize=True)['ok']
    assert len(calls) == 2


def test_failed_and_dry_initialization_do_not_create_baseline(journal, sync_settings, monkeypatch):
    calls = []
    def execute(argv, timeout):
        calls.append(argv)
        Path(argv[argv.index('--workdir') + 1]).mkdir(exist_ok=True)
        return {'returncode': 1, 'stdout': '', 'stderr': 'disconnected'}
    monkeypatch.setattr('research_journal.sync.execute', execute)
    assert not run(journal.config, sync_settings, initialize=True, dry_run=True)['ok']
    assert not (journal.config.state/'sync/cluster/initialized.json').exists()
    assert not Path(calls[0][calls[0].index('--workdir') + 1]).exists()


def test_paths_or_filter_changes_require_review(journal, sync_settings, monkeypatch):
    monkeypatch.setattr('research_journal.sync.execute', lambda *a: {'returncode': 0, 'stdout': '', 'stderr': ''})
    assert run(journal.config, sync_settings, initialize=True)['ok']
    data = json.loads(sync_settings.read_text())
    data['hosts'][0]['path'] = '/home/user/Other Vault'
    sync_settings.write_text(json.dumps(data))
    result = run(journal.config, sync_settings)
    assert not result['ok'] and 'paths or filters changed' in result['hosts'][0]['error']


def test_access_marker_and_lock_prevent_unsafe_or_overlapping_runs(journal, sync_settings):
    (journal.config.vault/'RCLONE_TEST').unlink()
    with pytest.raises(ValueError, match='access marker'):
        run(journal.config, sync_settings)
    with exclusive(journal.config.state/'sync'):
        with pytest.raises(ValueError, match='already running'):
            with exclusive(journal.config.state/'sync'):
                pass


def test_conflict_copies_stay_out_of_task_search(journal, sync_settings, monkeypatch):
    monkeypatch.setattr('research_journal.sync.execute', lambda *a: {'returncode': 0, 'stdout': '', 'stderr': ''})
    conflict = journal.config.vault/'TODO list.md.rj-conflict-remote1'
    conflict.write_text('- [ ] conflict-only task\n')
    result = run(journal.config, sync_settings, initialize=True)
    assert result['conflicts'] == [conflict.name]
    assert not journal.search_tasks('conflict-only task')
    note = journal.config.path(journal.config.folder+'/Inbox/Sync conflicts.md')
    assert conflict.name in note.read_text()
    assert run(journal.config, sync_settings, status=True)['last_run']['ok']


@pytest.mark.parametrize('change', [
    {'name': '-oProxyCommand=bad'}, {'path': '/'}, {'path': '../vault'},
    {'backup': '/home/user/Obsidian Vault/inside'}, {'backup': '/home/user'},
    {'path': '/home/user/../vault'}, {'path': '/home/user/vault\nnewsetting'},
])
def test_reject_unsafe_host_paths(sync_settings, change):
    data = json.loads(sync_settings.read_text())
    data['hosts'][0].update(change)
    sync_settings.write_text(json.dumps(data))
    with pytest.raises(ValueError):
        settings(sync_settings)


def test_filters_limit_sync_to_notes_and_conflicts(journal):
    rules = filters(journal.config)
    assert '+ *.md\n' in rules
    assert '+ *.md.rj-conflict-*' in rules
    assert '- .*/**' in rules and '- **' in rules
    assert '/Research Journal/Home.md' in rules
    assert '/Research Journal/Reviews/Index.md' in rules


def test_projection_failure_is_reported_after_successful_transfer(journal, sync_settings, monkeypatch):
    from research_journal.storage import Conflict
    monkeypatch.setattr('research_journal.sync.execute', lambda *a: {'returncode': 0, 'stdout': '', 'stderr': ''})
    def broken(*args):
        raise Conflict('generated marker needs repair')
    monkeypatch.setattr('research_journal.sync.Journal.refresh', broken)
    result = run(journal.config, sync_settings, initialize=True)
    assert not result['ok'] and 'marker needs repair' in result['refresh_error']
    assert (journal.config.state/'sync/cluster/initialized.json').exists()
    assert not run(journal.config, sync_settings, status=True)['last_run']['ok']
