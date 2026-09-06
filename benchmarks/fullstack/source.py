"""Carry forward agent-authored source, never DBs, evaluator runs or builds."""
import hashlib
from pathlib import Path
import shutil


def copy_source(source, destination):
    source=Path(source).resolve(strict=True);destination=Path(destination).resolve()
    if source==destination or source in destination.parents or destination in source.parents:
        raise ValueError('Source and destination must not overlap')
    files=[]
    for name in ('app.py','build.py','README.md','tsconfig.json','web','tests'):
        root=source/name
        if not root.exists() and not root.is_symlink():continue
        entries=[root]+list(root.rglob('*')) if root.is_dir() and not root.is_symlink() else [root]
        for path in entries:
            if path.is_symlink():raise ValueError('Source symlinks are not copied')
            if path.is_file() and path.suffix in ('.py','.ts','.js','.html','.css','.md','.json'):
                files.append(path)
    result=[]
    for path in sorted(files):
        relative=path.relative_to(source);target=destination/relative;target.parent.mkdir(parents=True,exist_ok=True)
        shutil.copyfile(path,target)
        result.append({'path':str(relative),'sha256':hashlib.sha256(target.read_bytes()).hexdigest()})
    return result
