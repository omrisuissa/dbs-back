#!/usr/bin/env python

from datetime import timedelta
import json

from flask import Flask, jsonify, request, abort
from flask.ext.mongoengine import MongoEngine, ValidationError
from flask.ext.security import Security, MongoEngineUserDatastore, \
    UserMixin, RoleMixin, login_required
from flask.ext.security.utils import encrypt_password, verify_password
from flask.ext.cors import CORS
from flask_jwt import JWT, JWTError, jwt_required, verify_jwt
from  flask.ext.jwt import current_user

from werkzeug import secure_filename

import pymongo

from utils import get_conf, get_logger, gen_missing_keys_error, upload_file, \
    get_oid
import phonetic


# Create app
app = Flask(__name__)

# Get configuration from file
conf = get_conf()

# Set app config
app.config['DEBUG'] = True
app.config['SECRET_KEY'] = conf.secret_key
app.config['SECURITY_PASSWORD_HASH'] = conf.security_password_hash
app.config['SECURITY_PASSWORD_SALT'] = conf.security_password_salt
app.config['JWT_EXPIRATION_DELTA'] = timedelta(days=1)

# DB Config
app.config['MONGODB_DB'] = conf.db_name
app.config['MONGODB_HOST'] = conf.db_host
app.config['MONGODB_PORT'] = conf.db_port

# Logging config
logger = get_logger()

#allow CORS
cors = CORS(app, origins=['*'], headers=['content-type', 'accept', 'Authorization'])

# Set up the JWT Token authentication
jwt = JWT(app)
@jwt.authentication_handler
def authenticate(username, password):
    user_obj = user_datastore.find_user(email=username)
    if not user_obj:
        logger.debug('User %s not found' % username)
        return None

    if verify_password(password, user_obj.password):
        # make user.id jsonifiable
        user_obj.id = str(user_obj.id)
        return user_obj
    else:
        logger.debug('Wrong password for %s' %  username)
        return None

@jwt.user_handler
def load_user(payload):
    user_obj = user_datastore.find_user(id=payload['user_id'])
    return user_obj

# Create database connection object
db = MongoEngine(app)
data_db = pymongo.Connection()['bhp6']

class Role(db.Document, RoleMixin):
    name = db.StringField(max_length=80, unique=True)
    description = db.StringField(max_length=255)

class User(db.Document, UserMixin):
    email = db.StringField(max_length=255)
    password = db.StringField(max_length=255)
    active = db.BooleanField(default=True)
    confirmed_at = db.DateTimeField()
    roles = db.ListField(db.ReferenceField(Role))

class Mjs(db.Document):
    mjs = db.ListField()

# Ensure we have a user to test with
@app.before_first_request
def setup_users():
    for role_name in ('user', 'admin'):
        if not user_datastore.find_role(role_name):
            logger.debug('Creating role %s' % role_name)
            user_datastore.create_role(name=role_name)

    user_role = user_datastore.find_role('user')
    if not user_datastore.get_user('tester@example.com'):
        logger.debug('Creating test user.')
        user_datastore.create_user(email='tester@example.com',
                                   password=encrypt_password('password'),
                                   roles=[user_role])

# Setup Flask-Security
user_datastore = MongoEngineUserDatastore(db, User, Role)
security = Security(app, user_datastore)


# Stubs for custom error handlers
@app.errorhandler(400)
def custom_400(error):
    response = humanify({'error': error.description})
    return response, 400

@app.errorhandler(403)
def custom_403(error):
    response = humanify({'error': error.description})
    return response, 403

@app.errorhandler(404)
def custom_404(error):
    response = humanify({'error': error.description})
    return response, 404

@app.errorhandler(405)
def custom_405(error):
    response = humanify({'error': error.description})
    return response, 405

@app.errorhandler(409)
def custom_409(error):
    response = humanify({'error': error.description})
    return response, 409

@app.errorhandler(500)
def custom_500(error):
    response = humanify({'error': error.description})
    return response, 500

# Utility functions
def humanify(obj):
    'Adds newline to Json responses to make CLI debugging easier'
    if type(obj) == list:
        return json.dumps(obj, indent=2) + '\n'
    else:
        resp = jsonify(obj)
        resp.set_data(resp.data+'\n')
        return resp

def is_admin(flask_user_obj):
    if flask_user_obj.has_role('admin'):
        return True
    else:
        return False

def mask_dict(from_dict, allowed_keys):
    'Return only allowed keys'
    rv = {}
    for key in allowed_keys:
        if from_dict.has_key(key):
            rv[key] = from_dict[key]
    return rv

def dictify(m_engine_object):
    return json.loads(m_engine_object.to_json())

# User management
def user_handler(user_id, method, data):
    if data:
        try:
            data = json.loads(data)
            if type(data) != dict:
                abort(400, 'Only dict like objects are supported for user management')
        except ValueError:
            e_message = 'Could not decode JSON from data'
            logger.debug(e_message)
            abort(400, e_message)

    if method == 'GET':
        return humanify(get_user(user_id))

    elif method == 'POST':
        return humanify(create_user(data))

    elif method == 'PUT':
        return humanify(update_user(user_id, data))

    elif method == 'DELETE':
        return humanify(delete_user(user_id))

def _get_user_or_error(user_id):
    user = user_datastore.get_user(user_id)
    if user:
        return user
    else:
        raise abort(404, 'User not found')

def _clean_user(user_obj):
    user_dict = dictify(user_obj)
    allowed_fields = ['_id', 'email']
    masked_user_dict = mask_dict(user_dict, allowed_fields)
    return masked_user_dict

def get_user(user_id):
    user_obj = _get_user_or_error(user_id)
    return _clean_user(user_obj)

def delete_user(user_id):
    user = _get_user_or_error(user_id)
    if is_admin(user):
        return {'error': 'God Mode!'}
    else:
        user.delete()
        return {}

def create_user(user_dict):
    try:
        email = user_dict['email']
        enc_password = encrypt_password(user_dict['password'])
    except KeyError as e:
        e_message = '%s key is missing from data' % e
        logger.debug(e_message)
        abort(400, e_message)

    user_exists = user_datastore.get_user(email)
    if user_exists:
        e_message = 'User %s with email %s already exists' % (str(user_exists.id), email)
        logger.debug(e_message)
        abort(409, e_message)

    created = user_datastore.create_user(email=email,
                                        password=enc_password)
    # Add default role to a newly created user
    user_datastore.add_role_to_user(created, 'user')

    return _clean_user(created)

def update_user(user_id, user_dict):
    user_obj = _get_user_or_error(user_id)
    if 'email' in user_dict.keys():
        user_obj.email = user_dict['email']
    if 'password' in user_dict.keys():
        enc_password = encrypt_password(user_dict['password'])
        user_obj.password = enc_password

    user_obj.save()
    return _clean_user(user_obj)

######################################################################################

def get_mjs(user_oid):
    mjs = Mjs.objects(id=user_oid).first()
    if mjs:
        mjs_dict = dictify(mjs)
        return humanify(mjs_dict)
    else:
        logger.debug('Mjs not found for user {}'.format(str(user_oid)))
        return humanify({'mjs':[]})

def update_mjs(user_oid, data):
    new_mjs = Mjs(id=user_oid, mjs = data)
    try:
        new_mjs.save()
        print dir(new_mjs)
        return humanify(dictify(new_mjs))
    except ValidationError as e:
        logger.debug('Error occured while saving mjs data for user {}'.format(str(user_oid)))
        logger.debug(e.message) 
        abort(500, e.message)

def fetch_items(item_list):
    if len(item_list) == 1:
        return _fetch_item(item_list[0])
    else:
        rv = []
        for item in item_list:
            if item: # Drop empty items
                rv.append( _fetch_item(item))
        return rv

def _fetch_item(item_id):
    if not '.' in item_id: # Need colection.id to unpack
        return None
    collection, _id = item_id.split('.')[:2]
    oid = get_oid(_id)
    if not oid:
        return {}

    item = data_db[collection].find_one(oid)
    return _make_serializable(item)

def _make_serializable(obj):
    # Make problematic fields Json serializable
    if obj.has_key('_id'):
        obj['_id'] = str(obj['_id'])
    if obj.has_key('UpdateDate'):
        obj['UpdateDate'] = str(obj['UpdateDate'])
    return obj

def search_by_header(string, collection):
    if phonetic.is_hebrew(string):
        lang = 'He'
    else:
        lang = 'En'
    item = data_db[collection].find_one({'Header.%s' % lang: string.upper()})
    if item:
        return _make_serializable(item)
    else:
        return {}

def get_completion(collection,string):
    pass

def get_contains(collection,string):
    pass

def get_phonetic(collection,string):
    pass


# Views
@app.route('/')
def home():
    # Check if the user is authenticated with JWT 
    try:
        verify_jwt()
        return humanify({'access': 'private'})

    except JWTError as e:
        logger.debug(e.description)
        return humanify({'access': 'public'})

@app.route('/private')
@jwt_required()
def private_space():
    return humanify({'access': 'private'})

@app.route('/user', methods=['GET', 'POST', 'PUT', 'DELETE'])
@app.route('/user/<user_id>', methods=['GET', 'POST', 'PUT', 'DELETE'])
def manage_user(user_id=None):
    '''
    Manage user accounts. If routed as /user, gives access only to logged in
    user, if routed as /user/<user_id>, allows administrative level access
    if the looged in user is in the admin group.
    POST gets special treatment, as there must be a way to register new user.
    '''
    try:
        verify_jwt()
    except JWTError as e:
        # You can create a new user while not being logged in
        # Will have to defend this endpoint with rate limiting or similar
        if request.method == 'POST':
            return user_handler(None, request.method, request.data)
        else:
            logger.debug(e.description)
            abort(403)

    if user_id:
        # admin access_mode
        if is_admin(current_user):
            return user_handler(user_id, request.method, request.data)
        else:
            logger.debug('Non-admin user %s tried to access user id %s' % (
                                                current_user.email, user_id))
            abort(403)
    else:
        # user access_mode
        user_id = str(current_user.id)
        return user_handler(user_id, request.method, request.data)

@app.route('/mjs', methods=['GET', 'PUT'])
@jwt_required()
def manage_jewish_story():
    '''Logged in user may GET or PUT their mjs metadata (a list).
    Each metadata member is a string in form of "collection_name.id".
    A PUT request must include ALL the metadata, not just a new object!
    The data is saved as an object in the mjs collection while its _id
    equals to this of the updating user.
    '''
    user_oid = current_user.id
    if request.method == 'GET':
        mjs = get_mjs(user_oid)
        return mjs

    elif request.method == 'PUT':
        try:
            data = json.loads(request.data)
        except ValueError:
            e_message = 'Could not decode JSON from data'
            logger.debug(e_message)
            abort(400, e_message)
        return update_mjs(user_oid, data)

@app.route('/upload', methods=['POST'])
@jwt_required()
def save_user_content():
    '''Logged in user POSTs a multipart request that includes a binary
    file and metadata.
    The server stores the metadata in a ugc collection and uploads the file
    to a bucket.
    '''
    if not request.files:
        abort(400, 'No files present!')

    must_have_keys = set(['title',
                        'description',
                        'location',
                        'date',
                        'creator_name',
                        'people_present'])

    form = request.form
    keys = form.keys()
    missing_keys = list(must_have_keys.difference(set(keys)))
    if missing_keys != []:
        e_message = gen_missing_keys_error(missing_keys)
        abort(400, e_message)

    user_oid = current_user.id
    file_obj = request.files['file']
    filename = secure_filename(file_obj.filename)
    metadata = dict(form)
    metadata['user_id'] = str(user_oid)
    metadata['filename'] = filename

    bucket = 'test_bucket'
    creds = ('foo', 'bar')
    saved = upload_file(file_obj, bucket, creds, metadata)
    if saved:
        return humanify({'md': metadata})
    else:
        abort(500, 'Failed to save %s' % filename)

@app.route('/search')
def general_search():
    pass

@app.route('/wsearch')
def wizard_search():
    args = request.args
    must_have_keys = set(['place', 'name'])
    keys = args.keys()
    missing_keys = list(must_have_keys.difference(set(keys)))
    if missing_keys != []:
        e_message = gen_missing_keys_error(missing_keys)
        abort(400, e_message)

    place_doc = search_by_header(args['place'], 'places')
    name_doc = search_by_header(args['name'], 'familyNames')
    return humanify({'place': place_doc, 'name': name_doc})



@app.route('/suggest/<collection>/<string>')
def get_suggestions(collection,string):
    '''
    This view returns a Json with 3 fields:
    "complete", "contains", "phonetic".
    Each field holds a list of up to 5 strings.
    '''
    rv = {}
    rv['complete'] = get_completion(collection,string)
    rv['contains'] = get_contains(collection,string)
    rv['complete'] = get_phonetic(collection,string)
    return rv


@app.route('/item/<item_id>')
def get_items(item_id):
    '''
    This view returns either Json representing an item or a list of such Jsons.
    The expected item_id string is in form of "collection_name.item_id"
    and could be  split by commas - if there is only one id, the view will return
    a single Json. 
    Only the first 10 ids will be returned for the list view to prevent abuse.
    '''
    items_list = item_id.split(',')
    items = fetch_items(items_list[:10])
    if items:
        return humanify(items)
    else:
        abort(404, 'Nothing found ;(')

if __name__ == '__main__':
    logger.debug('Starting api')
    app.run()
