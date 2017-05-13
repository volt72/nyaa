import flask
from werkzeug.datastructures import CombinedMultiDict
from nyaa import app, db
from nyaa import models, forms
from nyaa import bencode, utils
from nyaa import torrents
from nyaa import backend
from nyaa import api_handler
from nyaa.search import search_elastic, search_db
import config

import json
from datetime import datetime, timedelta
from ipaddress import ip_address
import os.path
import base64
from urllib.parse import quote
import math
from werkzeug import url_encode

from itsdangerous import URLSafeSerializer, BadSignature

import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formatdate

from flask_paginate import Pagination


DEBUG_API = False
DEFAULT_MAX_SEARCH_RESULT = 1000
DEFAULT_PER_PAGE = 75
SERACH_PAGINATE_DISPLAY_MSG = ('Displaying results {start}-{end} out of {total} results.<br>\n'
                               'Please refine your search results if you can\'t find '
                               'what you were looking for.')


def redirect_url():
    url = flask.request.args.get('next') or \
        flask.request.referrer or \
        '/'
    if url == flask.request.url:
        return '/'
    return url


@app.template_global()
def modify_query(**new_values):
    args = flask.request.args.copy()

    for key, value in new_values.items():
        args[key] = value

    return '{}?{}'.format(flask.request.path, url_encode(args))


@app.template_global()
def filter_truthy(input_list):
    ''' Jinja2 can't into list comprehension so this is for
        the search_results.html template '''
    return [item for item in input_list if item]

def search(term='', user=None, sort='id', order='desc', category='0_0', quality_filter='0', page=1, rss=False, admin=False):
    sort_keys = {
        'id': models.Torrent.id,
        'size': models.Torrent.filesize,
        'name': models.Torrent.display_name,
        'seeders': models.Statistic.seed_count,
        'leechers': models.Statistic.leech_count,
        'downloads': models.Statistic.download_count
    }

    sort_ = sort.lower()
    if sort_ not in sort_keys:
        flask.abort(400)
    sort = sort_keys[sort]

    order_keys = {
        'desc': 'desc',
        'asc': 'asc'
    }

    order_ = order.lower()
    if order_ not in order_keys:
        flask.abort(400)

    filter_keys = {
        '0': None,
        '1': (models.TorrentFlags.REMAKE, False),
        '2': (models.TorrentFlags.TRUSTED, True),
        '3': (models.TorrentFlags.COMPLETE, True)
    }

    sentinel = object()
    filter_tuple = filter_keys.get(quality_filter.lower(), sentinel)
    if filter_tuple is sentinel:
        flask.abort(400)

    if user:
        user = models.User.by_id(user)
        if not user:
            flask.abort(404)
        user = user.id

    main_category = None
    sub_category = None
    main_cat_id = 0
    sub_cat_id = 0
    if category:
        cat_match = re.match(r'^(\d+)_(\d+)$', category)
        if not cat_match:
            flask.abort(400)

        main_cat_id = int(cat_match.group(1))
        sub_cat_id = int(cat_match.group(2))

        if main_cat_id > 0:
            if sub_cat_id > 0:
                sub_category = models.SubCategory.by_category_ids(main_cat_id, sub_cat_id)
            else:
                main_category = models.MainCategory.by_id(main_cat_id)

            if not category:
                flask.abort(400)

    # Force sort by id desc if rss
    if rss:
        sort = sort_keys['id']
        order = 'desc'
        page = 1

    same_user = False
    if flask.g.user:
        same_user = flask.g.user.id == user

    if term:
        query = db.session.query(models.TorrentNameSearch)
    else:
        query = models.Torrent.query

    # Filter by user
    if user:
        query = query.filter(models.Torrent.uploader_id == user)
        # If admin, show everything
        if not admin:
            # If user is not logged in or the accessed feed doesn't belong to user,
            # hide anonymous torrents belonging to the queried user
            if not same_user:
                query = query.filter(models.Torrent.flags.op('&')(
                    int(models.TorrentFlags.ANONYMOUS | models.TorrentFlags.DELETED)).is_(False))

    if main_category:
        query = query.filter(models.Torrent.main_category_id == main_cat_id)
    elif sub_category:
        query = query.filter((models.Torrent.main_category_id == main_cat_id) &
                             (models.Torrent.sub_category_id == sub_cat_id))

    if filter_tuple:
        query = query.filter(models.Torrent.flags.op('&')(int(filter_tuple[0])).is_(filter_tuple[1]))

    # If admin, show everything
    if not admin:
        query = query.filter(models.Torrent.flags.op('&')(
            int(models.TorrentFlags.HIDDEN | models.TorrentFlags.DELETED)).is_(False))

    if term:
        for item in shlex.split(term, posix=False):
            if len(item) >= 2:
                query = query.filter(FullTextSearch(
                    item, models.TorrentNameSearch, FullTextMode.NATURAL))

    # Sort and order
    if sort.class_ != models.Torrent:
        query = query.join(sort.class_)

    query = query.order_by(getattr(sort, order)())

    if rss:
        query = query.limit(app.config['RESULTS_PER_PAGE'])
    else:
        query = query.paginate_faste(page, per_page=app.config['RESULTS_PER_PAGE'], step=5)

    return query


@app.template_global()
def category_name(cat_id):
    ''' Given a category id (eg. 1_2), returns a category name (eg. Anime - English-translated) '''
    return ' - '.join(get_category_id_map().get(cat_id, ['???']))


@app.errorhandler(404)
def not_found(error):
    return flask.render_template('404.html'), 404


@app.before_request
def before_request():
    flask.g.user = None
    if 'user_id' in flask.session:
        user = models.User.by_id(flask.session['user_id'])
        if not user:
            return logout()

        flask.g.user = user

        if 'timeout' not in flask.session or flask.session['timeout'] < datetime.now():
            flask.session['timeout'] = datetime.now() + timedelta(days=7)
            flask.session.permanent = True
            flask.session.modified = True

        if flask.g.user.status == models.UserStatusType.BANNED:
            return 'You are banned.', 403


def _generate_query_string(term, category, filter, user):
    params = {}
    if term:
        params['q'] = str(term)
    if category:
        params['c'] = str(category)
    if filter:
        params['f'] = str(filter)
    if user:
        params['u'] = str(user)
    return params


@app.template_filter('utc_time')
def get_utc_timestamp(datetime_str):
    ''' Returns a UTC POSIX timestamp, as seconds '''
    UTC_EPOCH = datetime.utcfromtimestamp(0)
    return int((datetime.strptime(datetime_str, '%Y-%m-%dT%H:%M:%S') - UTC_EPOCH).total_seconds())


@app.template_filter('display_time')
def get_display_time(datetime_str):
    return datetime.strptime(datetime_str, '%Y-%m-%dT%H:%M:%S').strftime('%Y-%m-%d %H:%M')


@utils.cached_function
def get_category_id_map():
    ''' Reads database for categories and turns them into a dict with
        ids as keys and name list as the value, ala
        {'1_0': ['Anime'], '1_2': ['Anime', 'English-translated'], ...} '''
    cat_id_map = {}
    for main_cat in models.MainCategory.query:
        cat_id_map[main_cat.id_as_string] = [main_cat.name]
        for sub_cat in main_cat.sub_categories:
            cat_id_map[sub_cat.id_as_string] = [main_cat.name, sub_cat.name]
    return cat_id_map


# Routes start here #


app.register_blueprint(api_handler.api_blueprint, url_prefix='/api')


def chain_get(source, *args):
    ''' Tries to return values from source by the given keys.
        Returns None if none match.
        Note: can return a None from the source. '''
    sentinel = object()
    for key in args:
        value = source.get(key, sentinel)
        if value is not sentinel:
            return value
    return None


@app.route('/rss', defaults={'rss': True})
@app.route('/', defaults={'rss': False})
def home(rss):
    render_as_rss = rss
    req_args = flask.request.args
    if req_args.get('page') == 'rss':
        render_as_rss = True

    search_term = chain_get(req_args, 'q', 'term')

    sort_key = req_args.get('s')
    sort_order = req_args.get('o')

    category = chain_get(req_args, 'c', 'cats')
    quality_filter = chain_get(req_args, 'f', 'filter')

    user_name = chain_get(req_args, 'u', 'user')
    page_number = chain_get(req_args, 'p', 'page', 'offset')
    try:
        page_number = max(1, int(page_number))
    except (ValueError, TypeError):
        page_number = 1

    # Check simply if the key exists
    use_magnet_links = 'magnets' in req_args or 'm' in req_args

    results_per_page = app.config.get('RESULTS_PER_PAGE', DEFAULT_PER_PAGE)

    user_id = None
    if user_name:
        user = models.User.by_username(user_name)
        if not user:
            flask.abort(404)
        user_id = user.id

    query_args = {
        'user': user_id,
        'sort': sort_key or 'id',
        'order': sort_order or 'desc',
        'category': category or '0_0',
        'quality_filter': quality_filter or '0',
        'page': page_number,
        'rss': render_as_rss,
        'per_page': results_per_page
    }

    if flask.g.user:
        query_args['logged_in_user'] = flask.g.user
        if flask.g.user.is_moderator:  # God mode
            query_args['admin'] = True

    # If searching, we get results from elastic search
    use_elastic = app.config.get('USE_ELASTIC_SEARCH')
    if use_elastic and search_term:
        query_args['term'] = search_term

        max_search_results = app.config.get('ES_MAX_SEARCH_RESULT', DEFAULT_MAX_SEARCH_RESULT)

        # Only allow up to (max_search_results / page) pages
        max_page = min(query_args['page'], int(math.ceil(max_search_results / results_per_page)))

        query_args['page'] = max_page
        query_args['max_search_results'] = max_search_results

        query_results = search_elastic(**query_args)

        if render_as_rss:
            return render_rss(
                '"{}"'.format(search_term), query_results,
                use_elastic=True, magnet_links=use_magnet_links)
        else:
            rss_query_string = _generate_query_string(
                search_term, category, quality_filter, user_name)
            max_results = min(max_search_results, query_results['hits']['total'])
            # change p= argument to whatever you change page_parameter to or pagination breaks
            pagination = Pagination(p=query_args['page'], per_page=results_per_page,
                                    total=max_results, bs_version=3, page_parameter='p',
                                    display_msg=SERACH_PAGINATE_DISPLAY_MSG)
            return flask.render_template('home.html',
                                         use_elastic=True,
                                         pagination=pagination,
                                         torrent_query=query_results,
                                         search=query_args,
                                         rss_filter=rss_query_string)
    else:
        # If ES is enabled, default to db search for browsing
        if use_elastic:
            query_args['term'] = ''
        else:  # Otherwise, use db search for everything
            query_args['term'] = search_term or ''

        query = search_db(**query_args)
        if render_as_rss:
            return render_rss('Home', query, use_elastic=False, magnet_links=use_magnet_links)
        else:
            rss_query_string = _generate_query_string(
                search_term, category, quality_filter, user_name)
            # Use elastic is always false here because we only hit this section
            # if we're browsing without a search term (which means we default to DB)
            # or if ES is disabled
            return flask.render_template('home.html',
                                         use_elastic=False,
                                         torrent_query=query,
                                         search=query_args,
                                         rss_filter=rss_query_string)


@app.route('/user/<user_name>', methods=['GET', 'POST'])
def view_user(user_name):
    user = models.User.by_username(user_name)

    if not user:
        flask.abort(404)

    admin_form = None
    if flask.g.user and flask.g.user.is_moderator and flask.g.user.level > user.level:
        admin_form = forms.UserForm()
        default, admin_form.user_class.choices = _create_user_class_choices(user)
        if flask.request.method == 'GET':
            admin_form.user_class.data = default

    if flask.request.method == 'POST' and admin_form and admin_form.validate():
        selection = admin_form.user_class.data

        if selection == 'regular':
            user.level = models.UserLevelType.REGULAR
        elif selection == 'trusted':
            user.level = models.UserLevelType.TRUSTED
        elif selection == 'moderator':
            user.level = models.UserLevelType.MODERATOR

        db.session.add(user)
        db.session.commit()

        return flask.redirect(flask.url_for('view_user', user_name=user.username))

    user_level = ['Regular', 'Trusted', 'Moderator', 'Administrator'][user.level]

    req_args = flask.request.args

    search_term = chain_get(req_args, 'q', 'term')

    sort_key = req_args.get('s')
    sort_order = req_args.get('o')

    category = chain_get(req_args, 'c', 'cats')
    quality_filter = chain_get(req_args, 'f', 'filter')

    page_number = chain_get(req_args, 'p', 'page', 'offset')
    try:
        page_number = max(1, int(page_number))
    except (ValueError, TypeError):
        page_number = 1

    results_per_page = app.config.get('RESULTS_PER_PAGE', DEFAULT_PER_PAGE)

    query_args = {
        'term': search_term or '',
        'user': user.id,
        'sort': sort_key or 'id',
        'order': sort_order or 'desc',
        'category': category or '0_0',
        'quality_filter': quality_filter or '0',
        'page': page_number,
        'rss': False,
        'per_page': results_per_page
    }

    if flask.g.user:
        query_args['logged_in_user'] = flask.g.user
        if flask.g.user.is_moderator:  # God mode
            query_args['admin'] = True

    # Use elastic search for term searching
    rss_query_string = _generate_query_string(search_term, category, quality_filter, user_name)
    use_elastic = app.config.get('USE_ELASTIC_SEARCH')
    if use_elastic and search_term:
        query_args['term'] = search_term

        max_search_results = app.config.get('ES_MAX_SEARCH_RESULT', DEFAULT_MAX_SEARCH_RESULT)

        # Only allow up to (max_search_results / page) pages
        max_page = min(query_args['page'], int(math.ceil(max_search_results / results_per_page)))

        query_args['page'] = max_page
        query_args['max_search_results'] = max_search_results

        query_results = search_elastic(**query_args)

        max_results = min(max_search_results, query_results['hits']['total'])
        # change p= argument to whatever you change page_parameter to or pagination breaks
        pagination = Pagination(p=query_args['page'], per_page=results_per_page,
                                total=max_results, bs_version=3, page_parameter='p',
                                display_msg=SERACH_PAGINATE_DISPLAY_MSG)
        return flask.render_template('user.html',
                                     use_elastic=True,
                                     pagination=pagination,
                                     torrent_query=query_results,
                                     search=query_args,
                                     user=user,
                                     user_page=True,
                                     rss_filter=rss_query_string,
                                     level=user_level,
                                     admin_form=admin_form)
    # Similar logic as home page
    else:
        if use_elastic:
            query_args['term'] = ''
        else:
            query_args['term'] = search_term or ''
        query = search_db(**query_args)
        return flask.render_template('user.html',
                                     use_elastic=False,
                                     torrent_query=query,
                                     search=query_args,
                                     user=user,
                                     user_page=True,
                                     rss_filter=rss_query_string,
                                     level=user_level,
                                     admin_form=admin_form)


@app.template_filter('rfc822')
def _jinja2_filter_rfc822(date, fmt=None):
    return formatdate(float(date.strftime('%s')))


@app.template_filter('rfc822_es')
def _jinja2_filter_rfc822(datestr, fmt=None):
    return formatdate(float(datetime.strptime(datestr, '%Y-%m-%dT%H:%M:%S').strftime('%s')))


def render_rss(label, query, use_elastic, magnet_links=False):
    rss_xml = flask.render_template('rss.xml',
                                    use_elastic=use_elastic,
                                    magnet_links=magnet_links,
                                    term=label,
                                    site_url=flask.request.url_root,
                                    torrent_query=query)
    response = flask.make_response(rss_xml)
    response.headers['Content-Type'] = 'application/xml'
    # Cache for an hour
    response.headers['Cache-Control'] = 'max-age={}'.format(1 * 5 * 60)
    return response


# @app.route('/about', methods=['GET'])
# def about():
    # return flask.render_template('about.html')


@app.route('/login', methods=['GET', 'POST'])
def login():
    if flask.g.user:
        return flask.redirect(redirect_url())

    form = forms.LoginForm(flask.request.form)
    if flask.request.method == 'POST' and form.validate():
        username = form.username.data.strip()
        password = form.password.data
        user = models.User.by_username(username)

        if not user:
            user = models.User.by_email(username)

        if (not user or password != user.password_hash
                or user.status == models.UserStatusType.INACTIVE):
            flask.flash(flask.Markup(
                '<strong>Login failed!</strong> Incorrect username or password.'), 'danger')
            return flask.redirect(flask.url_for('login'))

        user.last_login_date = datetime.utcnow()
        user.last_login_ip = ip_address(flask.request.remote_addr).packed
        db.session.add(user)
        db.session.commit()

        flask.g.user = user
        flask.session['user_id'] = user.id
        flask.session.permanent = True
        flask.session.modified = True

        return flask.redirect(redirect_url())

    return flask.render_template('login.html', form=form)


@app.route('/logout')
def logout():
    flask.g.user = None
    flask.session.permanent = False
    flask.session.modified = False

    response = flask.make_response(flask.redirect(redirect_url()))
    response.set_cookie(app.session_cookie_name, expires=0)
    return response


@app.route('/register', methods=['GET', 'POST'])
def register():
    if flask.g.user:
        return flask.redirect(redirect_url())

    form = forms.RegisterForm(flask.request.form)
    if flask.request.method == 'POST' and form.validate():
        user = models.User(username=form.username.data.strip(),
                           email=form.email.data.strip(), password=form.password.data)
        user.last_login_ip = ip_address(flask.request.remote_addr).packed
        db.session.add(user)
        db.session.commit()

        if config.USE_EMAIL_VERIFICATION:  # force verification, enable email
            activ_link = get_activation_link(user)
            send_verification_email(user.email, activ_link)
            return flask.render_template('waiting.html')
        else:  # disable verification, set user as active and auto log in
            user.status = models.UserStatusType.ACTIVE
            db.session.add(user)
            db.session.commit()
            flask.g.user = user
            flask.session['user_id'] = user.id
            flask.session.permanent = True
            flask.session.modified = True
            return flask.redirect(redirect_url())

    return flask.render_template('register.html', form=form)


@app.route('/profile', methods=['GET', 'POST'])
def profile():
    if not flask.g.user:
        return flask.redirect('/')  # so we dont get stuck in infinite loop when signing out

    form = forms.ProfileForm(flask.request.form)

    level = ['Regular', 'Trusted', 'Moderator', 'Administrator'][flask.g.user.level]

    if flask.request.method == 'POST' and form.validate():
        user = flask.g.user
        new_email = form.email.data.strip()
        new_password = form.new_password.data

        if new_email:
            # enforce password check on email change too
            if form.current_password.data != user.password_hash:
                flask.flash(flask.Markup(
                    '<strong>Email change failed!</strong> Incorrect password.'), 'danger')
                return flask.redirect('/profile')
            user.email = form.email.data
            flask.flash(flask.Markup(
                '<strong>Email successfully changed!</strong>'), 'success')
        if new_password:
            if form.current_password.data != user.password_hash:
                flask.flash(flask.Markup(
                    '<strong>Password change failed!</strong> Incorrect password.'), 'danger')
                return flask.redirect('/profile')
            user.password_hash = form.new_password.data
            flask.flash(flask.Markup(
                '<strong>Password successfully changed!</strong>'), 'success')

        db.session.add(user)
        db.session.commit()

        flask.g.user = user
        return flask.redirect('/profile')

    _user = models.User.by_id(flask.g.user.id)
    username = _user.username
    current_email = _user.email

    return flask.render_template('profile.html', form=form, name=username, email=current_email,
                                 level=level)


@app.route('/user/activate/<payload>')
def activate_user(payload):
    s = get_serializer()
    try:
        user_id = s.loads(payload)
    except BadSignature:
        flask.abort(404)

    user = models.User.by_id(user_id)

    if not user:
        flask.abort(404)

    user.status = models.UserStatusType.ACTIVE

    db.session.add(user)
    db.session.commit()

    return flask.redirect('/login')


@utils.cached_function
def _create_upload_category_choices():
    ''' Turns categories in the database into a list of (id, name)s '''
    choices = [('', '[Select a category]')]
    id_map = get_category_id_map()

    for key in sorted(id_map.keys()):
        cat_names = id_map[key]
        is_main_cat = key.endswith('_0')

        # cat_name = is_main_cat and cat_names[0] or (' - ' + cat_names[1])
        cat_name = ' - '.join(cat_names)
        choices.append((key, cat_name, is_main_cat))
    return choices


@app.route('/upload', methods=['GET', 'POST'])
def upload():
    upload_form = forms.UploadForm(CombinedMultiDict((flask.request.files, flask.request.form)))
    upload_form.category.choices = _create_upload_category_choices()

    if flask.request.method == 'POST' and upload_form.validate():
        torrent = backend.handle_torrent_upload(upload_form, flask.g.user)

        return flask.redirect('/view/' + str(torrent.id))
    else:
        # If we get here with a POST, it means the form data was invalid: return a non-okay status
        status_code = 400 if flask.request.method == 'POST' else 200
        return flask.render_template('upload.html', upload_form=upload_form), status_code


@app.route('/view/<int:torrent_id>')
def view_torrent(torrent_id):
    torrent = models.Torrent.by_id(torrent_id)
    form = forms.CommentForm()

    viewer = flask.g.user

    if not torrent:
        flask.abort(404)

    # Only allow admins see deleted torrents
    if torrent.deleted and not (viewer and viewer.is_moderator):
        flask.abort(404)

    # Only allow owners and admins to edit torrents
    can_edit = viewer and (viewer is torrent.user or viewer.is_moderator)

    files = None
    if torrent.filelist:
        files = json.loads(torrent.filelist.filelist_blob.decode('utf-8'))

    comments = models.Comment.query.filter_by(torrent=torrent_id)
    comment_count = comments.count()

    return flask.render_template('view.html', torrent=torrent,
                                 files=files,
                                 viewer=viewer,
                                 form=form,
                                 comments=comments,
                                 comment_count=comment_count,
                                 can_edit=can_edit)


@app.route('/view/<int:torrent_id>/submit_comment', methods=['POST'])
def submit_comment(torrent_id):
    form = forms.CommentForm(flask.request.form)

    if flask.request.method == 'POST' and form.validate():
        comment_text = (form.comment.data or '').strip()

        # Null entry for User just means Anonymous
        current_user_id = flask.g.user.id if flask.g.user else None
        comment = models.Comment(
            torrent=torrent_id,
            user_id=current_user_id,
            text=comment_text)

        db.session.add(comment)
        db.session.commit()

    return flask.redirect(flask.url_for('view_torrent', torrent_id=torrent_id))

@app.route('/view/<int:torrent_id>/delete_comment/<int:comment_id>')
def delete_comment(torrent_id, comment_id):
    if flask.g.user is not None and flask.g.user.is_admin:
        models.Comment.query.filter_by(id=comment_id).delete()
        db.session.commit()
    else:
        flask.abort(403)
    
    return flask.redirect(flask.url_for('view_torrent', torrent_id=torrent_id))     


@app.route('/view/<int:torrent_id>/edit', methods=['GET', 'POST'])
def edit_torrent(torrent_id):
    torrent = models.Torrent.by_id(torrent_id)
    form = forms.EditForm(flask.request.form)
    form.category.choices = _create_upload_category_choices()

    editor = flask.g.user

    if not torrent:
        flask.abort(404)

    # Only allow admins edit deleted torrents
    if torrent.deleted and not (editor and editor.is_moderator):
        flask.abort(404)

    # Only allow torrent owners or admins edit torrents
    if not editor or not (editor is torrent.user or editor.is_moderator):
        flask.abort(403)

    if flask.request.method == 'POST' and form.validate():
        # Form has been sent, edit torrent with data.
        torrent.main_category_id, torrent.sub_category_id = \
            form.category.parsed_data.get_category_ids()
        torrent.display_name = (form.display_name.data or '').strip()
        torrent.information = (form.information.data or '').strip()
        torrent.description = (form.description.data or '').strip()

        torrent.hidden = form.is_hidden.data
        torrent.remake = form.is_remake.data
        torrent.complete = form.is_complete.data
        torrent.anonymous = form.is_anonymous.data

        if editor.is_trusted:
            torrent.trusted = form.is_trusted.data
        if editor.is_moderator:
            torrent.deleted = form.is_deleted.data

        db.session.commit()

        flask.flash(flask.Markup(
            'Torrent has been successfully edited! Changes might take a few minutes to show up.'),
            'info')

        return flask.redirect(flask.url_for('view_torrent', torrent_id=torrent.id))
    else:
        if flask.request.method != 'POST':
            # Fill form data only if the POST didn't fail
            form.category.data = torrent.sub_category.id_as_string
            form.display_name.data = torrent.display_name
            form.information.data = torrent.information
            form.description.data = torrent.description

            form.is_hidden.data = torrent.hidden
            form.is_remake.data = torrent.remake
            form.is_complete.data = torrent.complete
            form.is_anonymous.data = torrent.anonymous

            form.is_trusted.data = torrent.trusted
            form.is_deleted.data = torrent.deleted

        return flask.render_template('edit.html',
                                     form=form,
                                     torrent=torrent,
                                     editor=editor)


@app.route('/view/<int:torrent_id>/magnet')
def redirect_magnet(torrent_id):
    torrent = models.Torrent.by_id(torrent_id)

    if not torrent:
        flask.abort(404)

    return flask.redirect(torrents.create_magnet(torrent))


@app.route('/view/<int:torrent_id>/torrent')
def download_torrent(torrent_id):
    torrent = models.Torrent.by_id(torrent_id)

    if not torrent:
        flask.abort(404)

    resp = flask.Response(_get_cached_torrent_file(torrent))
    resp.headers['Content-Type'] = 'application/x-bittorrent'
    resp.headers['Content-Disposition'] = 'inline; filename*=UTF-8\'\'{}'.format(
        quote(torrent.torrent_name.encode('utf-8')))

    return resp


def _get_cached_torrent_file(torrent):
    # Note: obviously temporary
    cached_torrent = os.path.join(app.config['BASE_DIR'],
                                  'torrent_cache', str(torrent.id) + '.torrent')
    if not os.path.exists(cached_torrent):
        with open(cached_torrent, 'wb') as out_file:
            out_file.write(torrents.create_bencoded_torrent(torrent))

    return open(cached_torrent, 'rb')


def get_serializer(secret_key=None):
    if secret_key is None:
        secret_key = app.secret_key
    return URLSafeSerializer(secret_key)


def get_activation_link(user):
    s = get_serializer()
    payload = s.dumps(user.id)
    return flask.url_for('activate_user', payload=payload, _external=True)


def send_verification_email(to_address, activ_link):
    ''' this is until we have our own mail server, obviously.
     This can be greatly cut down if on same machine.
     probably can get rid of all but msg formatting/building,
     init line and sendmail line if local SMTP server '''

    msg_body = 'Please click on: ' + activ_link + ' to activate your account.\n\n\nUnsubscribe:'

    msg = MIMEMultipart()
    msg['Subject'] = 'Verification Link'
    msg['From'] = config.MAIL_FROM_ADDRESS
    msg['To'] = to_address
    msg.attach(MIMEText(msg_body, 'plain'))

    server = smtplib.SMTP(config.SMTP_SERVER, config.SMTP_PORT)
    server.set_debuglevel(1)
    server.ehlo()
    server.starttls()
    server.ehlo()
    server.login(config.SMTP_USERNAME, config.SMTP_PASSWORD)
    server.sendmail(config.SMTP_USERNAME, to_address, msg.as_string())
    server.quit()


def _create_user_class_choices(user):
    choices = [('regular', 'Regular')]
    default = 'regular'
    if flask.g.user:
        if flask.g.user.is_moderator:
            choices.append(('trusted', 'Trusted'))
        if flask.g.user.is_superadmin:
            choices.append(('moderator', 'Moderator'))

        if user:
            if user.is_moderator:
                default = 'moderator'
            elif user.is_trusted:
                default = 'trusted'

    return default, choices


# #################################### STATIC PAGES ####################################
@app.route('/rules', methods=['GET'])
def site_rules():
    return flask.render_template('rules.html')


@app.route('/help', methods=['GET'])
def site_help():
    return flask.render_template('help.html')


# #################################### API ROUTES ####################################
@app.route('/api/upload', methods=['POST'])
def api_upload():
    is_valid_user, user, debug = api_handler.validate_user(flask.request)
    if not is_valid_user:
        return flask.make_response(flask.jsonify({"Failure": "Invalid username or password."}), 400)
    api_response = api_handler.api_upload(flask.request, user)
    return api_response
