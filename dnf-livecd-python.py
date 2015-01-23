#!/usr/bin/python3
import argparse
import fnmatch
import json
import logging
import os
import subprocess

import dnf

lgr = logging.getLogger(__name__)
lgr.setLevel(logging.DEBUG)
ch = logging.StreamHandler()
ch.setLevel(logging.DEBUG)
lgr.addHandler(ch)

here = os.path.dirname(__file__)
repoopts = ['--repoid', 'rawhide']
repoopts_source = ['--repoid', 'rawhide-source']

def do_run(cmd):
    lgr.debug('Running: ' + ' '.join(cmd))
    return subprocess.Popen(cmd,
                            stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE).communicate()

def checkout_repo(which='ks', do_checkout=True):
    which_to_repo_and_url = {
        'ks': ('spin-kickstarts', 'https://git.fedorahosted.org/git/spin-kickstarts.git'),
        'lorax': ('lorax', 'https://git.fedorahosted.org/git/lorax.git'),
        'atomic': ('fedora-atomic', 'https://git.fedorahosted.org/git/fedora-atomic.git'),
    }
    dir, url = which_to_repo_and_url[which]
    dir = os.path.join(here, dir)
    if not do_checkout:
        return dir
    if not os.path.isdir(dir):
        to_run = ['git', 'clone', url]
        do_run(to_run)
    else:
        to_run = ['git', '-C', dir, 'pull']
        do_run(to_run)

    return dir

def load_deps_from_ks(ks_dir, ks_name):
    """Get top dependencies from given kickstart in given dir."""
    # we need to do the set difference in the top level, since one kickstart can
    # exclude something from different kickstart
    add, exclude = _load_deps_from_ks(ks_dir, ks_name)
    return add, exclude

def _load_deps_from_ks(ks_dir, ks_name):
    """Get 2-tuple (dependencies to add, dependencies to exclude) - from given ks in given dir."""
    add_deps = set()
    excl_deps = set()
    ks_lines = open(os.path.join(ks_dir, ks_name), 'r').readlines()
    inside_packages = False
    for l in ks_lines:
        line = l.strip()
        if line.startswith('%packages'):
            inside_packages = True
            continue
        elif line.startswith('%end'):
            inside_packages = False
            continue
        elif line.startswith('%include'):
            incl_ks = line.split()[1]
            add, exclude = _load_deps_from_ks(ks_dir, incl_ks)
            add_deps.update(add)
            excl_deps.update(exclude)
            continue
        if inside_packages:
            if not line or line.startswith('#'):
                continue
            comment_start = line.find('#')
            if comment_start != -1:
                line = line[:comment_start]
            line = line.strip()
            if line.startswith('@'):
                add_deps.add(line)
            elif line.startswith('-'):
                excl_deps.add(line[1:])
            else:
                add_deps.add(line)
    return (add_deps, excl_deps)


def load_deps_from_lorax(lt_dir, lt_name):
    lt_path = os.path.join(lt_dir, lt_name)
    lt_lines = open(lt_path, 'r').readlines()
    ret = []
    for line in lt_lines:
        if line.startswith('installpkg'):
            ret.extend(line.split()[1:])

    return ret


def load_deps_from_ostree_manifest(om_dir, om_name):
    om_path = os.path.join(om_dir, om_name)
    om = json.load(open(om_path))
    return om['packages'] + om['bootstrap_packages']


def resolve_python_reverse_deps(to_add, to_exclude, env_group_optionals):
    base = dnf.Base()
    base.conf.cachedir = '/tmp'
    base.conf.substitutions['releasever'] = 22
    repo = dnf.repo.Repo('rawhide', '/tmp')
    repo.metalink = 'https://mirrors.fedoraproject.org/metalink?repo=rawhide&arch=x86_64'
    base.repos.add(repo)
    base.fill_sack(load_system_repo=False)
    base.read_comps()

    for d in to_add:
        if d.startswith('@') and not _package_excluded(d, to_exclude):
            if d.startswith('@^'):
                env_group = base.comps.environment_by_pattern(d[2:])
                groups = env_group.mandatory_groups
                if env_group_optionals:
                    groups.extend(env_group.optional_groups)
            else:
                groups = [base.comps.group_by_pattern(d[1:])]
            # we can't use group_install with "exclude" parameter, see
            #  https://bugzilla.redhat.com/show_bug.cgi?id=1131969#c11 and c12
            for group in groups:
                if group is None:
                    lgr.error('Group not found :"{0}". Skipping ...'.format(d))
                    continue
                elif _package_excluded('@' + group.id, to_exclude):
                    # if we got the group from env group, it's still possible we may want to skip
                    continue
                for pkg in list(group.default_packages) + list(group.mandatory_packages):
                    if not _package_excluded(pkg.name, to_exclude):
                        try:
                            base.install(pkg.name)
                        except dnf.exceptions.MarkingError:
                            lgr.error('Couldn\'t find "{pkg}"'.format(pkg=pkg.name))
        elif not _package_excluded(d, to_exclude):
            base.install(d)
    base.resolve()

    names = set()
    for transaction_item in base.transaction:
        for pkg in transaction_item.installs():
            for req in pkg.requires:
                if 'python' in str(req) or 'pygtk' in str(req):
                    names.add(pkg.name)
                    break
    return list(names)


def _package_excluded(pkg_name, exclude_list):
    return any(fnmatch.fnmatchcase(pkg_name, e) for e in exclude_list)


def get_srpms_for_python_reverse_deps(python_reverse_deps):
    """Find srpm names corresponding to given binary RPMs.

    Returns:
        mapping of srpms to corresponding binary rpms found on livecd
        for example:
            {'foo': 'foo-libs', 'foo-python', ...}
    """
    req_python = {}
    # preserve the order here so that we see the progress during run
    for dep in sorted(python_reverse_deps):
        to_run = ['repoquery', '--srpm', '--qf', '%{name}', dep] +\
                 repoopts + repoopts_source
        stdout, stderr = do_run(to_run)
        # sometimes this seems to return multiple identical lines
        srpms = stdout.decode('utf-8').splitlines()
        for srpm in srpms:
            req_python.setdefault(srpm, set())
            req_python[srpm].add(dep)
    return req_python


def get_srpms_that_br_python3(srpms):
    # find out if the srpms require "*python3*" for their build - if so, we'll mark them ok
    req_python3 = {}
    # preserve the order here so that we see the progress during run
    for dep in sorted(srpms):
        to_run = ['repoquery', '--archlist=src', '--requires', dep] + repoopts_source
        stdout, stderr = do_run(to_run)
        if 'python3' in stdout.decode('utf-8'):
            req_python3[dep] = srpms[dep]
    return req_python3


def get_good_and_bad_srpms(ks_name=None, ks_path=None, lt_name=None, om_name=None,
        env_group_optionals=False):
    # TODO: argument checking - must have precisely one
    if ks_name or ks_path:
        if ks_name:
            ks_dir = checkout_repo()
        elif ks_path:
            ks_dir, ks_name = os.path.split(ks_path)
        top_deps_add, top_deps_exclude = load_deps_from_ks(ks_dir, ks_name)
    elif lt_name:
        lt_dir = checkout_repo(which='lorax')
        top_deps_add, top_deps_exclude = load_deps_from_lorax(lt_dir, lt_name), []
    else:  # om_name
        om_dir = checkout_repo(which='atomic')
        top_deps_add, top_deps_exclude = load_deps_from_ostree_manifest(om_dir, om_name), []
    lgr.debug('Adding: ' + str(sorted(top_deps_add)))
    lgr.debug('Excluding: ' + str(sorted(top_deps_exclude)))

    python_reverse_deps = resolve_python_reverse_deps(top_deps_add,
        top_deps_exclude, env_group_optionals)
    lgr.debug('Python reverse deps: ' + str(sorted(python_reverse_deps)))

    srpms_req_python = get_srpms_for_python_reverse_deps(python_reverse_deps)
    srpms_req_python3 = get_srpms_that_br_python3(srpms_req_python)

    # remove all the python3-ported rpms from srpms_req_python
    for good in srpms_req_python3:
        srpms_req_python.pop(good)
    return srpms_req_python3, srpms_req_python

def print_srpm(srpm, with_rpms):
    print(srpm[0], end='')
    if with_rpms:
        print(': ' + ' '.join(srpm[1]), end='')
    print()

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group(required=False)
    group.add_argument('-k', '--kickstart',
        help='Name of kickstart file from official spin-kickstarts repo.',
        default=None)
    group.add_argument('-p', '--kickstart-by-path',
        help='Absolute/relative path to a kickstart.',
        default=None)
    group.add_argument('-l', '--lorax-template',
        help='Name of lorax template from lorax repo, e.g. share/runtime-install.tmpl',
        default=None)
    group.add_argument('-o', '--ostree-manifest',
        help='Name of ostree manifest from fedora-atomic repo, e.g. fedora-atomic-docker-host.json',
        default=None)
    parser.add_argument('-b', '--binary-rpms',
        help='In addition to SRPMs, also print names of binary RPMs.',
        default=False,
        action='store_true')
    parser.add_argument('--env-group-optionals',
        help='Add optional groups from environment groups.',
        default=False,
        action='store_true')
    args = parser.parse_args()
    if not args.kickstart and not args.kickstart_by_path and not args.lorax_template and \
            not args.ostree_manifest:
        args.kickstart = 'fedora-live-workstation.ks'
    good, bad = get_good_and_bad_srpms(ks_name=args.kickstart, ks_path=args.kickstart_by_path,
        lt_name=args.lorax_template, om_name=args.ostree_manifest,
        env_group_optionals=args.env_group_optionals)

    print('----- Good -----')
    for srpm in sorted(good.items()):
        print_srpm(srpm, with_rpms=args.binary_rpms)

    print()
    print('----- Bad -----')
    for srpm in sorted(bad.items()):
        print_srpm(srpm, with_rpms=args.binary_rpms)
