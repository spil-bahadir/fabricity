from __future__ import with_statement

import posixpath
import time

from fabric.api import *
from fabric import contrib

from deploy.servers import *

env.disable_known_hosts = True

def bootstrap():
    """
    Bootstrap this project.

    """
    require('server', 'target_dir')
    setup()
    run('mkdir -p %s' % env.target_dir)
    with cd(env.target_dir):
        run('mkdir -p sql')
        run('mkdir -p revs')
        run('touch revs/deployed.txt')
        run('mkdir -p settings')
        run('mkdir -p migrations')
        run('mkdir -p synced')
        run('mkdir -p maintenance')
    run('mkdir -p %s' % env.server.settings['SITE_MEDIA_ROOT'])
    with cd(env.server.settings['SITE_MEDIA_ROOT']):
        run('mkdir -p media')
        env.server.make_web_writable('media')
        run('mkdir -p static')
        run('mkdir -p static/CACHE')
        env.server.make_web_writable('static/CACHE')
    with settings(warn_only=True):
        env.server.create_db_user()
        env.server.create_database()
        clone()
    configure()
    deploy()
    # too dangerous if db already exists: do this manually instead
    #loaddata()

def deploy():
    """
    Deploy last-checked-in version of this project to the server.

    """
    clean_pyc()
    update()
    try:
        build_env()
        build_settings()
        build_static()
        migrate()
    except:
        if contrib.console.confirm('error encountered: rollback?'):
            rollback()
        else:
            raise

    reload_code()

def deployed():
    """
    Print the log entry for the current deployed changeset.

    """
    require('code_dir')
    with cd(env.code_dir):
        run("hg parents")

def rollback():
    """
    Rollback to previously-deployed version.

    """
    require('code_dir')

    set_hgrev()

    with cd(env.target_dir):
        prevrev = run('head -n -1 revs/deployed.txt | tail -n 1')
    if not prevrev:
        abort('No previously deployed rev found, cannot roll back.')

    prevmigrations = posixpath.join(env.target_dir, 'migrations', prevrev)
    if contrib.files.exists(prevmigrations):
        with settings(warn_only=True):
            with cd(env.code_dir):
                apps = run("./manage migrate --list | grep '^[^ ]'").split()
        for app in apps:
            # get the last applied migration for this app in previous state
            latest = run("cat %s "
                         "| sed -ne '/^%s/,/^$/ {s/   \* //; p}' "
                         "| egrep '^[0-9]{4}' "
                         "| tail -n 1 "
                        "| cut -d _ -f 1"
                         % (prevmigrations, app))
            with cd(env.code_dir):
                if latest:
                    run('./manage migrate %s %s' % (app, latest))
                else:
                    run('./manage migrate %s 0001' % app)

    with cd(env.code_dir):
        run('hg up %s' % prevrev)

    with cd(env.target_dir):
        run('echo "%s" > revs/rolledback.txt' % env.hgrev)
        run('TMPFILE=`mktemp` '
            '&& cat revs/deployed.txt | head -n -1 > $TMPFILE '
            '&& mv $TMPFILE revs/deployed.txt')

    prevsettings = posixpath.join(env.target_dir, 'settings', prevrev)
    if contrib.files.exists(prevsettings):
        run('cp %s %s' % (prevsettings,
                          posixpath.join(env.code_dir,
                                         'etc', '92_local.conf')))

    build_env()
    build_static()
    reload_code()

def set_hgrev():
    """
    Set env.hgrev to the current deployed code revision.

    """
    with settings(warn_only=True):
        with cd(env.code_dir):
            env.hgrev = run('hg parents --template="{node}"')
        with cd(env.target_dir):
            last_deployed = run('tail -n 1 revs/deployed.txt')

    if env.hgrev != last_deployed:
        abort('Server rev %s does not match last-deployed rev %s: '
              'Manual intervention required.' % (env.hgrev, last_deployed))

def clone():
    """
    Clone this project to the server.

    """
    require('host_string', 'code_dir')
    if not contrib.files.exists(env.code_dir):
        local('hg clone . ssh://%s/%s' % (env.host_string, env.code_dir))
        with cd(env.code_dir):
            run('hg update')
    else:
        warn('not cloning repo, %s already exists!' % env.code_dir)

def update():
    """
    Update code on server to version in current working dir.

    """
    require('code_dir')

    newrev = local('hg parents --template="{node}"')
    local('hg push -f ssh://%s/%s -r %s' % (env.host_string, env.code_dir,
                                         newrev))
    with cd(env.code_dir):
        run('hg revert --all')
        run('hg up %s' % newrev)
    with cd(env.target_dir):
        run('echo "%s" >> revs/deployed.txt' % newrev)

    set_hgrev()

def dumpsql():
    """
    Dump a timestamped SQL backup.

    """
    require('target_dir', 'server')
    outfile = posixpath.join(env.target_dir, 'sql', "%s.sql" % time.time())
    return env.server.dump_database(outfile=outfile)

def migrate():
    """
    Sync and migrate the database.

    """
    require('code_dir', 'hgrev')
    dumpsql()
    with cd(env.target_dir):
        prevrev = run('head -n -1 revs/deployed.txt | tail -n 1')
    migrated = []
    synced = []
    if prevrev:
        prevmigrations = posixpath.join(env.target_dir, 'migrations', prevrev)
        prevsynced = posixpath.join(env.target_dir, 'synced', prevrev)
        with settings(warn_only=True):
            migrated = run("cat %s | grep '^[^ ]'" % prevmigrations).split()
            synced = run("cat %s | grep '^ > ' | cut -c 4-" % prevsynced).split()
    with cd(env.code_dir):
        new_synced = posixpath.join(env.target_dir, 'synced', env.hgrev)
        run('./manage syncdb --noinput > %s' % new_synced)
        syncdb_out = run('cat %s' % new_synced)
        need_migrate = [line[3:].split('.')[-1] for line in syncdb_out.splitlines()
                        if line.startswith(' - ') and len(line) > 3]
        fake_initial = [app for app in need_migrate
                        if app not in migrated
                        and app in synced]
        for app in fake_initial:
            run('./manage migrate %s 0001 --fake' % app)
        run('./manage migrate --noinput')
        run('./manage migrate --list > %s'
            % posixpath.join(env.target_dir, 'migrations', env.hgrev))

def build_settings(settings_dict=None):
    """
    Create local settings overrides file.

    """
    require('code_dir', 'target_dir', 'hgrev')

    env.server.update_settings()

    settings_dict = settings_dict or env.server.settings
    settings_file = posixpath.join(env.code_dir, 'etc', '92_local.conf')

    # remove old settings file
    with settings(warn_only=True):
        run('rm %s' % settings_file)

    # build new settings file
    for key, val in settings_dict.items():
        run('echo "%s = %s" >> %s' % (key, repr(val), settings_file))

    # save backup revision-tagged copy
    run('cp %s %s' % (settings_file,
                      posixpath.join(env.target_dir,
                                     'settings', env.hgrev)))

def build_env():
    """
    Build virtualenv for deployment and install project dependencies
    in it.

    """
    require('code_dir', 'target_dir')
    with cd(env.code_dir):
        run('scripts/mkenv %s' % posixpath.join(env.target_dir, 'venv'))
        run('scripts/install-prod-deps -q')

def build_static():
    """
    Collect static assets for deployment.

    """
    require('code_dir')
    with cd(env.code_dir):
        run('./manage build_static --noinput')

def dumpdata():
    """
    Dump site data fixture from deployment to local project.

    """
    require('code_dir')
    try:
        tmp = run('mktemp -t obc-data-dump.XXXX')
        with cd(env.code_dir):
            run('scripts/dumpdata')
            run('tar -cjf %s fixtures/' % tmp)
            run('hg revert --all')
        localtmp = local('mktemp -t obc-data-dump.XXXX').strip()
        get(tmp, localtmp)
        local('tar -xjf %s' % localtmp)
    finally:
        try:
            run('rm %s' % tmp)
            local('rm %s' % localtmp)
        except:
            pass

def loaddata():
    """
    Load site data fixture in deployment.

    """
    require('code_dir')
    with cd(env.code_dir):
        run('scripts/loaddata')
    # loaddata can muck up the web-writable perms on media/
    with cd(env.server.settings['SITE_MEDIA_ROOT']):
        env.server.make_web_writable('media')

def clean_pyc():
    """
    Clean up .pyc files.

    """
    require('code_dir')
    with settings(warn_only=True):
        with cd(env.code_dir):
            run('find . -name "*.pyc" -delete')

def upload_ssl_cert():
    """
    Upload deploy/ssl/server.crt and deploy/ssl/server.key to ssl/ directory on
    server.

    """
    require('target_dir')
    ssl_dir = posixpath.join(env.target_dir, "ssl")
    run("mkdir -p %s" % ssl_dir)
    put("deploy/ssl/server.crt", posixpath.join(ssl_dir, "server.crt"))
    put("deploy/ssl/server.key", posixpath.join(ssl_dir, "server.key"))

# wrappers for server-related tasks

def install_system():
    """
    Install system-level dependencies.

    """
    require('server')
    env.server.install_system()

def setup():
    """
    Server-specific initial app setup.

    """
    require('server')
    env.server.setup()

def configure():
    """
    Server-specific configuration.

    """
    require('server')
    env.server.configure()

def enable():
    """
    Enable site config in webserver.

    """
    require('server')
    env.server.enable()

def disable():
    """
    Disable site config in webserver.

    """
    require('server')
    env.server.disable()

def maintenance_on():
    """
    Replace the site with a maintenance page.

    """
    require('server')
    env.server.maintenance_on()

def maintenance_off():
    """
    Replace the site with a maintenance page.

    """
    require('server')
    env.server.maintenance_off()

def reload_server():
    """
    Reload webserver(s).

    """
    require('server')
    env.server.reload_server()

def restart_server():
    """
    Restart webserver(s).

    """
    require('server')
    env.server.restart_server()

def reload_code():
    """
    Reload Python code.

    """
    require('server')
    env.server.reload_code()

def server_method(name):
    """
    Run an arbitrary method of env.server.

    """
    require('server')
    method = getattr(env.server, name)
    method()
