from core.validation.validation import TSValidationError, validate_request, sanitize_objectify_json, stringify_objectid_cursor
from core.api.crud import db
from bson.objectid import ObjectId
from core.validation.permissions import get_role_approval_step, check_datamine_permissions
from core.notifications import notifications
from core.config import schema 
import cherrypy, logging

def approval(criteria):
    
    """
    Approve or reject an expence
    
    POST /data/approval/
    
    Expects { 'project_id' : string, 'user_id' : string, 'expence_id|trip_id' : string, 'action': approve|reject, 'note' : string  }
    Returns { 'error' : string, 'status' : integer }
    """
    
    validate_request('approval', criteria)
    sanified_criteria = sanitize_objectify_json(criteria)
    check_datamine_permissions('approval', sanified_criteria)

    # Current user can approve only approvals with status >= conf_approval_flow.index(group)
    owner_status = get_role_approval_step(cherrypy.session['_ts_user']['group'])
    draft_status = get_role_approval_step('draft')

    # Is searching found_expence or trip
    exp_id = sanified_criteria.get('expence_id')
    trp_id = sanified_criteria.get('trip_id')
    expence_type = 'expences' if exp_id else 'trips'
    expence_id = exp_id if exp_id else trp_id
    
    # Define match 
    match_expence = { 
                        '_id' : ObjectId(criteria['project_id']),
                        '%s._id' % expence_type : ObjectId(expence_id),
                        
                        }
    
    # Define status limits only if is not adminstrator. Status should be owner_one or draft.
    if owner_status != 0:
        match_expence['$or'] = [ { '%s.status' % expence_type : owner_status}, 
                                  { '%s.status' % expence_type : draft_status} ]
        
    # Limit for user id
    user_id = sanified_criteria.get('user_id')
    if user_id:
        match_expence['%s.user_id' % expence_type] = user_id

    aggregation_pipe = [  
                        { '$unwind' : '$%s' % expence_type },
                        { '$match' : match_expence }
    ]

    cherrypy.log('%s' % (aggregation_pipe), context = 'TS.APPROVAL.aggregation_pipe', severity = logging.INFO)
    result = list(db.project.aggregate(aggregation_pipe))
            
    if not result:
        raise TSValidationError("Can't find selected expence")
    
    found_expence = result[0]
    
    original_found_expence = found_expence.copy()
    
    # Approved
    if sanified_criteria['action'] == 'approve':
        if found_expence[expence_type]['status'] > 0:
            found_expence[expence_type]['status'] = found_expence[expence_type]['status'] - 1
    # Rejected
    else:
        found_expence[expence_type]['status'] = -abs(found_expence[expence_type]['status'])

    if 'note' in sanified_criteria and sanified_criteria['note']:
        if not 'notes' in found_expence[expence_type]:
            found_expence[expence_type]['notes'] = []
        found_expence[expence_type]['notes'].append(sanified_criteria['note'])

    cherrypy.log('%s' % (found_expence), context = 'TS.APPROVALS.found_expence', severity = logging.INFO)
    
    # Pull the original element
    db.project.update({ '_id' : ObjectId(criteria['project_id'])}, { '$pull' : { expence_type : { '_id' : ObjectId(expence_id)} } } )
    # Push the modified element, with an hack to avoid to push the entire array
    db.project.update({ '_id' : ObjectId(criteria['project_id']) }, { '$push' : { expence_type : found_expence[expence_type] } } )
    
    approval_result = {}
    
    # Status
    approval_result['status'] = found_expence[expence_type]['status']
    
    # Approver data
    approver_data = dict((key,value) for key, value in cherrypy.session['_ts_user'].iteritems() if key in ('username', 'name', 'surname', 'email'))
    
    # Notifications
    notifications_result = notifications.notify_expence(found_expence, expence_type, approver_data)
    if notifications_result:
        approval_result['notifications'] = notifications_result
    
    return approval_result


def search_approvals(criteria):
    
    """
    Search expences
    
    POST /data/search_approvals/
    
    Expects { 'projects_id' : [ ], 'user_id': string, 'type': trips|expences|any, 'status': toapprove|approved|rejected|any  }
    Returns { 'error' : string, 'records' : [] }
    """
    
    validate_request('search_approvals', criteria)
    sanified_criteria = sanitize_objectify_json(criteria)
    check_datamine_permissions('search_approvals', sanified_criteria)

    # Get flow status number relative to current user
    owner_status = get_role_approval_step(cherrypy.session['_ts_user']['group'])

    # Search only expences or trips or both
    type_requested = sanified_criteria.get('type', 'any' )
    if type_requested == 'any':
        aggregations_types = [ 'trips', 'expences']
    else:
        aggregations_types = [ type_requested ]
    
    records = { 'trips' : [], 'expences' : [] }
    
    for aggregation_type in aggregations_types:

        # Prepare status filter
        status_requested = sanified_criteria.get('status', 'toapprove')

        # If is administrator, can see whole ranges
        if owner_status == 0:
            if status_requested == 'toapprove':
                match_project_status = { '%s.status' % aggregation_type : { '$gt' : 0 } }
            elif status_requested == 'approved':
                match_project_status = { '%s.status' % aggregation_type : 0 }
            elif status_requested == 'rejected':
                match_project_status = { '%s.status' % aggregation_type : { '$lt' : 0 } }
            else:
                match_project_status = { }             

        # If it is a permitted user, can see only specific status
        else:
            if status_requested == 'toapprove':
                match_project_status = { '%s.status' % aggregation_type : owner_status }
            elif status_requested == 'approved':
                match_project_status = { '%s.status' % aggregation_type : 0 }
            elif status_requested == 'rejected':
                match_project_status = { '%s.status' % aggregation_type : { '$lt' : 0 } }
            else:
                match_project_status = { '$or' : [  { '%s.status' % aggregation_type : owner_status }, 
                                                    { '%s.status' % aggregation_type : 0 },
                                                    { '%s.status' % aggregation_type : { '$lt' : 0 } }
                                                ] } 

        # If project_id is not set, allows only managed_projects
        projects_requested = [ ObjectId(p) for p in sanified_criteria.get('projects_id', cherrypy.session['_ts_user']['managed_projects']) ]
        if projects_requested:
            match_project_status.update({ '_id' : { '$in' : projects_requested } })

        user_id = sanified_criteria.get('user_id')
        if user_id:
            match_project_status.update({ '%s.user_id' % aggregation_type : user_id })

        project_rename = { '%s.project_id' % aggregation_type : '$_id' }
        for key in schema['project']['properties'][aggregation_type]['items']['properties'].keys():
            project_rename['%s.%s' % (aggregation_type, key)] = 1

        aggregation_pipe = [  
                            { '$unwind' : '$%s' % aggregation_type },
                            { '$match' : match_project_status },
                            { '$project' : project_rename },
                            { '$group' : { '_id' : '$%s' % (aggregation_type) } },
                            { '$sort' : { '_id.start' : 1, '_id.date' : 1 } }
        ]

        cherrypy.log('%s' % (aggregation_pipe), context = 'TS.SEARCH_APPROVALS.aggregation_pipe', severity = logging.INFO)

        aggregation_result = db.project.aggregate(aggregation_pipe)
        records[aggregation_type] = stringify_objectid_cursor([ record['_id'] for record in list(aggregation_result) ])

    return records
