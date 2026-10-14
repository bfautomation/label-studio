from label_studio.core import version


def test_read_py_returns_info_from_generated_version_file(tmp_path, monkeypatch):
    core_dir = tmp_path / 'core'
    core_dir.mkdir()
    (core_dir / 'version_.py').write_text("info = {'version': '1.2.3', 'commit': 'abc123'}\n")

    monkeypatch.setattr(version, '__file__', str(core_dir / 'version.py'))

    assert version._read_py() == {'version': '1.2.3', 'commit': 'abc123'}


def test_read_py_returns_empty_dict_when_generated_module_has_no_info(tmp_path, monkeypatch):
    core_dir = tmp_path / 'core'
    core_dir.mkdir()
    (core_dir / 'version_.py').write_text("value = 'missing info'\n")

    monkeypatch.setattr(version, '__file__', str(core_dir / 'version.py'))

    assert version._read_py() == {}


def test_get_git_commit_info_uses_generated_version_file_when_git_is_unavailable(tmp_path, monkeypatch):
    core_dir = tmp_path / 'core'
    core_dir.mkdir()
    generated_version = {'version': '9.9.9', 'commit': 'fallback-commit', 'branch': 'develop'}
    (core_dir / 'version_.py').write_text(f'info = {generated_version!r}\n')

    monkeypatch.setattr(version, '__file__', str(core_dir / 'version.py'))

    def fail_git(*args, **kwargs):
        raise version.CalledProcessError(127, kwargs.get('args', args[0] if args else 'git'))

    monkeypatch.setattr(version, 'run', fail_git)

    assert version.get_git_commit_info(ls=True) == generated_version


def test_get_git_commit_info_ignores_write_failures_when_git_is_unavailable(tmp_path, monkeypatch):
    core_dir = tmp_path / 'core'
    core_dir.mkdir()
    generated_version = {'version': '9.9.9', 'commit': 'fallback-commit', 'branch': 'develop'}
    (core_dir / 'version_.py').write_text(f'info = {generated_version!r}\n')

    monkeypatch.setattr(version, '__file__', str(core_dir / 'version.py'))

    def fail_git(*args, **kwargs):
        raise version.CalledProcessError(127, kwargs.get('args', args[0] if args else 'git'))

    monkeypatch.setattr(version, 'run', fail_git)
    monkeypatch.setattr(version, '_write_py', lambda info: (_ for _ in ()).throw(PermissionError('read-only')))

    assert version.get_git_commit_info(ls=True) == generated_version


def test_read_git_head_info_supports_gitdir_file_and_does_not_depend_on_cwd(tmp_path, monkeypatch):
    repo_root = tmp_path / 'repo'
    core_dir = repo_root / 'label_studio' / 'core'
    git_dir = repo_root / '.git-data'
    refs_dir = git_dir / 'refs' / 'heads'
    refs_dir.mkdir(parents=True)
    core_dir.mkdir(parents=True)

    (repo_root / '.git').write_text('gitdir: .git-data\n')
    (git_dir / 'HEAD').write_text('ref: refs/heads/develop\n')
    (refs_dir / 'develop').write_text('abc123def456\n')

    monkeypatch.setattr(version, '__file__', str(core_dir / 'version.py'))
    monkeypatch.chdir(tmp_path)

    assert version._read_git_head_info() == ('abc123def456', 'develop')


def test_read_git_head_info_reads_packed_refs(tmp_path, monkeypatch):
    repo_root = tmp_path / 'repo'
    core_dir = repo_root / 'label_studio' / 'core'
    git_dir = repo_root / '.git'
    git_dir.mkdir(parents=True)
    core_dir.mkdir(parents=True)

    (git_dir / 'HEAD').write_text('ref: refs/heads/develop\n')
    (git_dir / 'packed-refs').write_text('abc123def456 refs/heads/develop\n')

    monkeypatch.setattr(version, '__file__', str(core_dir / 'version.py'))
    monkeypatch.chdir(tmp_path)

    assert version._read_git_head_info() == ('abc123def456', 'develop')


def test_read_git_head_info_supports_worktree_commondir_and_branch_names_with_slashes(tmp_path, monkeypatch):
    repo_root = tmp_path / 'repo'
    worktree_root = tmp_path / 'worktree'
    core_dir = worktree_root / 'label_studio' / 'core'
    worktree_git_dir = repo_root / '.git' / 'worktrees' / 'label-studio-worktree'
    common_git_dir = repo_root / '.git'

    core_dir.mkdir(parents=True)
    worktree_git_dir.mkdir(parents=True)
    common_git_dir.mkdir(parents=True, exist_ok=True)

    (worktree_root / '.git').write_text(f'gitdir: {worktree_git_dir}\n')
    (worktree_git_dir / 'HEAD').write_text('ref: refs/heads/feature/upload-flow\n')
    (worktree_git_dir / 'commondir').write_text('../..\n')
    (common_git_dir / 'packed-refs').write_text('abc123def456 refs/heads/feature/upload-flow\n')

    monkeypatch.setattr(version, '__file__', str(core_dir / 'version.py'))
    monkeypatch.chdir(tmp_path)

    assert version._read_git_head_info() == ('abc123def456', 'feature/upload-flow')


def test_read_only_version_write_does_not_change_working_directory(tmp_path, monkeypatch):
    original_cwd = version.os.getcwd()
    monkeypatch.setattr(version, '__file__', str(tmp_path / 'missing' / 'version.py'))
    version._safe_write_py({'version': '1.2.3'})
    assert version.os.getcwd() == original_cwd


def test_partial_override_preserves_generated_release_metadata(tmp_path, monkeypatch):
    generated = {'version': '9.9.9', 'commit': 'abc123', 'branch': 'develop'}
    (tmp_path / 'version_.py').write_text(f'info = {generated!r}\n')
    monkeypatch.setattr(version, '__file__', str(tmp_path / 'version.py'))
    monkeypatch.setattr(version, 'BRANCH_OVERRIDE', 'feature/review')
    monkeypatch.setattr(version, 'run', lambda *args, **kwargs: (_ for _ in ()).throw(FileNotFoundError('git')))
    assert version.get_git_commit_info() == {**generated, 'branch': 'feature/review'}


def test_fallback_version_reads_declared_package_version_from_source_archive(tmp_path, monkeypatch):
    core_dir = tmp_path / 'label_studio' / 'core'
    core_dir.mkdir(parents=True)
    (tmp_path / 'pyproject.toml').write_text(
        '[project]\nname = "label-studio"\nversion = "2.3.4"\n[other]\nversion = "unrelated"\n'
    )
    monkeypatch.setattr(version, '__file__', str(core_dir / 'version.py'))
    monkeypatch.setattr(version, 'run', lambda *args, **kwargs: (_ for _ in ()).throw(FileNotFoundError('git')))
    assert version.get_git_commit_info()['version'] == '2.3.4'


def test_fallback_version_uses_installed_package_metadata(tmp_path, monkeypatch):
    monkeypatch.setattr(version, '__file__', str(tmp_path / 'version.py'))
    monkeypatch.setattr(version, 'package_version', lambda name: '3.4.5')
    assert version._package_version() == '3.4.5'


def test_read_git_head_info_respects_git_dir_pointer_override(tmp_path, monkeypatch):
    git_dir = tmp_path / 'git-data'
    git_dir.mkdir()
    (git_dir / 'HEAD').write_text('abc123\n')
    pointer = tmp_path / 'git-pointer'
    pointer.write_text('gitdir: git-data\n')
    monkeypatch.setenv('GIT_DIR', str(pointer))
    assert version._read_git_head_info() == ('abc123', '')
