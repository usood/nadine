import logging
import ssl, urllib, urllib2, base64
from datetime import datetime
from xml.etree import ElementTree

from django.conf import settings
from django.utils import timezone
from django.contrib.auth.models import User
from django.core.exceptions import ImproperlyConfigured

logger = logging.getLogger(__name__)

###############################################################################################################
# Communication Logic
###############################################################################################################

class DoorController:
    
    def __init__(self, ip_address, username, password):
        self.door_ip = ip_address
        self.door_user = username
        self.door_pass = password
        self.clear_data()

    def clear_data(self):
        self.cardholders_by_id = {}
        self.cardholders_by_username = {}

    def door_url(self):
        door_url = "https://%s/cgi-bin/vertx_xml.cgi" % self.door_ip
        return door_url

    def send_xml_str(self, xml_str):
        logger.debug("Sending: %s" % xml_str)
        
        xml_data = urllib.urlencode({'XML': xml_str})
        request = urllib2.Request(self.door_url(), xml_data)
        base64string = base64.encodestring('%s:%s' % (self.door_user, self.door_pass)).replace('\n', '')
        request.add_header("Authorization", "Basic %s" % base64string) 
        context = ssl._create_unverified_context()
        context.set_ciphers('RC4-SHA')
        
        result = urllib2.urlopen(request, context=context)
        return_code = result.getcode()
        return_xml = result.read()
        result.close()
        
        logger.debug("Response code: %d" % return_code)
        logger.debug("Response: %s" % return_xml)
        if return_code != 200:
            raise Exception("Did not receive 200 return code")
        error = get_attribute(return_xml, "errorMessage")
        if error:
            raise Exception("Received an error: %s" % error)
        
        return return_xml

    def send_xml(self, xml):
        xml_str = ElementTree.tostring(xml, encoding='utf8', method='xml')
        return self.send_xml_str(xml_str)
    
    def test_connection(self):
        test_xml = list_doors()
        self.send_xml(test_xml)

    def save_cardholder(self, cardholder):
        if 'cardholderID' in cardholder:
            self.cardholders_by_id[cardholder['cardholderID']] = cardholder
        if 'username' in cardholder:
            self.cardholders_by_username[cardholder['username']] = cardholder

    def load_cardholders(self):
        self.clear_data()
        offset = 0
        count = 10
        moreRecords = True
        while moreRecords:
            #logger.debug("offset: %d, count: %d" % (offset, count))
            xml_str = self.send_xml(list_cardholders(offset, count))
            xml = ElementTree.fromstring(xml_str)
            for child in xml[0]:
                person = child.attrib
                cardholderID = person['cardholderID']
                person['full_name'] = "%s %s " % (person['forename'], person['surname'])
                if 'custom1' in person:
                    username = person['custom1']
                    person['username'] = username
                self.save_cardholder(person)
            returned = int(xml[0].attrib['recordCount'])
            logger.debug("returned: %d cardholders" % returned)
            if count > returned:
                moreRecords = False
            else:
                offset = offset + count

    def get_cardholder_by_id(self, cardholderID):
        if cardholderID in self.cardholders_by_id:
            return self.cardholders_by_id[cardholderID]
        return None

    def get_cardholder_by_username(self, username):
        if username in self.cardholders_by_username:
            return self.cardholders_by_username[username]
        return None

    def load_credentials(self):
        # This method pulls all the credentials and infuses the cardholders with their card numbers.
        self.load_cardholders()
        offset = 0
        count = 10
        moreRecords = True
        while moreRecords:
            xml_str = self.send_xml(list_credentials(offset, count))
            xml = ElementTree.fromstring(xml_str)
            for child in xml[0]:
                card = child.attrib
                if 'cardholderID' in card:
                    cardholderID = card['cardholderID']
                    cardNumber = card['rawCardNumber']
                    cardholder = self.get_cardholder_by_id(cardholderID)
                    if cardholder:
                        cardholder['cardNumber'] = cardNumber
            returned = int(xml[0].attrib['recordCount'])
            logger.debug("returned: %d credentials" % returned)
            if count > returned:
                moreRecords = False
            else:
                offset = offset + count
        logger.debug(self.cardholders_by_id)

    def process_door_codes(self, door_codes, load_credentials=True):
        if load_credentials:
            self.load_credentials()
        
        changes = []
        for new_code in door_codes:
            username = new_code['username']
            cardholder = self.get_cardholder_by_username(username)
            #print "username: %s, cardholder: %s" % (username, cardholder)
            if cardholder:
                if cardholder['cardNumber'] != new_code['code']:
                    cardholder['action'] = 'change'
                    cardholder['new_code'] = new_code['code']
                    changes.append(cardholder)
                else:
                    cardholder['action'] = 'no_change'
            else:
                new_cardholder = {'action':'add', 'username':username}
                new_cardholder['forname'] = new_code['first_name']
                new_cardholder['surname'] = new_code['last_name']
                new_cardholder['full_name'] = "%s %s" % (new_code['first_name'], new_code['last_name'])
                new_cardholder['new_code'] = new_code['code']
                changes.append(new_cardholder)
        
        # Now loop through all the cardholders and any that don't have an action
        # are in the controller but not in the given list.  Remove them.
        for cardholder in self.cardholders_by_id.values():
            if not 'action' in cardholder:
                cardholder['action'] = 'delete'
                changes.append(cardholder)
        
        return changes

    def process_changes(self, change_list):
        for change in change_list:
            action = change['action']
            logger.debug("%s: %s" % (action, change['full_name']))
            if action == 'add':
                logger.debug("")
                self.add_cardholder(change['forname'], change['surname'], change['username'], change['new_code'])
            elif action == 'change':
                self.change_cardholder(change['cardholderID'], change['cardNumber'], change['new_code'])
            elif action == 'delete':
                self.delete_cardholder(change['cardholderID'], change['cardNumber'])
                pass

    def add_cardholder(self, forname, surname, username, cardNumber):
        response = self.send_xml(create_cardholder(forname, surname, username))
        cardholderID = get_attribute(response, 'cardholderID')
        logger.debug("New Cardholder: username: %s, cardholderID: %s" % (username, cardholderID)) 
        self.send_xml(create_credential(cardNumber))
        self.send_xml(assign_credential(cardholderID, cardNumber))
        self.send_xml(add_roleset(cardholderID))

    def change_cardholder(self, cardholderID, oldCardNumber, newCardNumber):
        self.send_xml(delete_credential(oldCardNumber))
        self.send_xml(create_credential(newCardNumber))
        self.send_xml(assign_credential(cardholderID, newCardNumber))

    def delete_cardholder(self, cardholderID, cardNumber):
        self.send_xml(delete_credential(cardNumber))
        self.send_xml(delete_cardholder(cardholderID))

    def pull_events(self, recordCount):
        # First pull the overview to get the current recordmarker and timestamp
        event_xml_str = self.send_xml(list_events())
        event_xml = ElementTree.fromstring(event_xml_str)
        rm = event_xml[0].attrib['currentRecordMarker']
        ts = event_xml[0].attrib['currentTimestamp']
        
        events = []
        event_xml_str = self.send_xml(list_events(recordCount, rm, ts))
        event_xml = ElementTree.fromstring(event_xml_str)
        for child in event_xml[0]:
            event_str = get_event_detail(child.attrib)
            events.append(event_str)
        return events


###############################################################################################################
# Helper Functions
###############################################################################################################

# Pull a specific attribute out of a big xml string
def get_attribute(xml_str, attribute):
    if attribute in xml_str:
        e_start = xml_str.index(attribute) + 14
        e_end = xml_str.index('"', e_start)
        return xml_str[e_start:e_end]

def send_xml(ip_address, username, password, xml_str):
    controller = DoorController(ip_address, username, password)
    return controller.send_xml_str(xml_str)

# def unlockDoor():
#     xml = door_command_xml("unlockDoor")
#     controller = DoorController()
#     return controller.send_xml(xml)
#
# def lockDoor():
#     xml = door_command_xml("lockDoor")
#     controller = DoorController()
#     return controller.send_xml(xml)

###############################################################################################################
# Base XML Functions
###############################################################################################################


def root_elm():
    return ElementTree.Element('VertXMessage')


###############################################################################################################
# Door Commands
###############################################################################################################


def doors_elm(action):
    root = root_elm()
    elm = ElementTree.SubElement(root, 'hid:Doors')
    elm.set('action', action)
    return (root, elm)

# <hid:Doors action="LR" responseFormat="status" />
def list_doors():
    root, elm = doors_elm('LR')
    elm.set('responseFormat', 'status')
    return root

# <hid:Doors action="CM" command="lockDoor"/>
# <hid:Doors action="CM" command="unlockDoor"/>
def door_command(command):
    root, elm = doors_elm('CM')
    elm.set('command', command)
    return root


###############################################################################################################
# Cardholder Commands
###############################################################################################################


# Parent element
def cardholders_parent_elm(action):
    root = root_elm()
    parent = ElementTree.SubElement(root, 'hid:Cardholders')
    parent.set('action', action)
    return (root, parent)

# Child element
def cardholder_child_elm(action):
    root, parent = cardholders_parent_elm(action)
    child = ElementTree.SubElement(parent, "hid:Cardholder")
    return (root, child)

# <hid:Cardholders action="LR" responseFormat="expanded" recordOffset="0" recordCount="10"/>
def list_cardholders(recordOffset, recordCount):
    root, elm = cardholders_parent_elm('LR')
    elm.set('responseFormat', 'expanded')
    elm.set('recordOffset', str(recordOffset))
    elm.set('recordCount', str(recordCount))
    return root

# <hid:Cardholders action="AD">
#     <hid:Cardholder forename="Test"
#                     middleName="M"
#                     surname="Aaaaaa"
#                     exemptFromPassback="true"
#                     extendedAccess="false"
#                     confirmingPin=""
#                     email="test@example.com"
#                     custom1="c1"
#                     custom2="c2"
#                     phone="800-555-1212" />
# </hid:Cardholders>
def create_cardholder(first_name, last_name, username):
    root, elm = cardholder_child_elm('AD')
    elm.set('forename', first_name)
    elm.set('surname', last_name)
    elm.set('custom1', username)
    return root

# <hid:Cardholders action="DD" cardholderID="647"/>
def delete_cardholder(cardholderID):
    root, elm = cardholders_parent_elm('DD')
    elm.set('cardholderID', cardholderID)
    return root


###############################################################################################################
# Credential Commands
###############################################################################################################


# Parent element
def credentials_parent_elm(action):
    root = root_elm()
    parent = ElementTree.SubElement(root, 'hid:Credentials')
    parent.set('action', action)
    return (root, parent)

# Child element
def credential_child_elm(action):
    root, parent = credentials_parent_elm(action)
    child = ElementTree.SubElement(parent, "hid:Credential")
    return (root, child)

# <hid:Credentials action="LR" responseFormat="expanded" recordOffset="0" recordCount="10"/>
def list_credentials(recordOffset, recordCount):
    root, elm = credentials_parent_elm('LR')
    elm.set('responseFormat', 'expanded')
    elm.set('recordOffset', str(recordOffset))
    elm.set('recordCount', str(recordCount))
    return root

# <hid:Credentials action="AD">
#     <hid:Credential cardNumber="012345AB" isCard="true" endTime="2015-06-28T23:59:59"/>
# </hid:Credentials>
def create_credential(cardNumber):
    root, child = credential_child_elm('AD')
    child.set('isCard', 'true')
    child.set('cardNumber', cardNumber)
    return root

# <hid:Credentials action="UD" rawCardNumber="012345AB" isCard="true">
#     <hid:Credential cardholderID="647"/>
# </hid:Credentials>
def assign_credential(cardholderID, cardNumber):
    root, parent = credentials_parent_elm('UD')
    parent.set('isCard', 'true')
    parent.set('rawCardNumber', cardNumber)
    child = ElementTree.SubElement(parent, "hid:Credential")
    child.set('cardholderID', cardholderID)
    return root

# <hid:Credentials action="DD" rawCardNumber="111111" isCard="true"></hid:Credentials>
def delete_credential(cardNumber):
    root, parent = credentials_parent_elm('DD')
    parent.set('isCard', 'true')
    parent.set('rawCardNumber', cardNumber)
    return root

###############################################################################################################
# Event Commands
###############################################################################################################


def event_elm(action):
    root = root_elm()
    elm = ElementTree.SubElement(root, 'hid:EventMessages')
    elm.set('action', action)
    return (root, elm)

# <hid:EventMessages action="DR"/>
def display_recent():
    root, elm = event_elm('DR')
    return root

# <hid:EventMessages action="LR" recordCount="1" historyRecordMarker="2233" historyTimestamp="1428541357"/>
def list_events(recordCount=None, recordMarker=None, timestamp=None):
    root, elm = event_elm('LR')
    if recordCount:
        elm.set('recordCount', str(recordCount))
    if recordMarker:
        elm.set('historyRecordMarker', str(recordMarker))
    if timestamp:
        elm.set('historyTimestamp', str(timestamp))
    return root

event_details = {}
event_details["1022"] = "Denied Access - Card Not Found"
event_details["1023"] = "Denied Access - Access PIN Not Found"
event_details["2020"] = "Granted Access"
event_details["2021"] = "Granted Access -  Extended Time"
event_details["2024"] = "Denied Access - Schedule "
event_details["2029"] = "Denied Access - Wrong PIN"
event_details["2036"] = "Denied Access - Card Expired"
event_details["2042"] = "Denied Access - PIN Lockout"
event_details["2043"] = "Denied Access - Unassigned Card"
event_details["2044"] = "Denied Access - Unassigned Access PIN"
event_details["2046"] = "Denied Access - PIN Expired"
event_details["4034"] = "Alarm Acknowledged"
event_details["4035"] = "Door Locked-Scheduled"
event_details["4036"] = "Door Unlocked-Scheduled"
event_details["4041"] = "Door Forced Alarm"
event_details["4042"] = "Door Held Alarm"
event_details["4043"] = "Tamper Switch Alarm"
event_details["4044"] = "Input A Alarm"
event_details["4045"] = "Input B Alarm"
event_details["7020"] = "Time Set"
event_details["12031"] = "Granted Access - Manual"
event_details["12032"] = "Door Unlocked"
event_details["12033"] = "Door Locked"

def get_event_detail(event_dict):
    type = event_dict['eventType']
    # The two most common events  
    if type == "1022":
        event_text = "Card Not Found (%s) " % event_dict['rawCardNumber']
    elif type == "2020":
        event_text = "Access Granted (%s %s)" % (event_dict['forename'], event_dict['surname'])
    else:
        event_text = event_details[type]
    return "%s: %s" % (event_dict['timestamp'], event_text)

#<?xml version="1.0" encoding="UTF-8"?><VertXMessage xmlns:hid="http://www.hidcorp.com/VertX"><hid:EventMessages action="RL" historyRecordMarker="0" historyTimestamp="947256029" recordCount="16" moreRecords="false" ><hid:EventMessage readerAddress="0" ioState="0" eventType="4034" timestamp="2000-01-20T02:22:57" /><hid:EventMessage readerAddress="0" rawCardNumber="010F2B8E" eventType="1022" timestamp="2000-01-01T00:13:34" /><hid:EventMessage readerAddress="0" cardholderID="1" forename="Jacob" surname="Sayles" eventType="2020" timestamp="2000-01-01T00:13:23" /><hid:EventMessage readerAddress="0" rawCardNumber="009B4C9B" eventType="1022" timestamp="2000-01-01T00:13:11" /><hid:EventMessage readerAddress="0" commandStatus="true" eventType="12031" timestamp="2000-01-01T00:13:02" /><hid:EventMessage readerAddress="0" commandStatus="true" eventType="12033" timestamp="2000-01-01T00:12:58" /><hid:EventMessage readerAddress="0" rawCardNumber="010F2B8E" eventType="1022" timestamp="2000-01-01T00:12:14" /><hid:EventMessage readerAddress="0" rawCardNumber="009B4C9B" eventType="1022" timestamp="2000-01-01T00:12:05" /><hid:EventMessage readerAddress="0" cardholderID="1" forename="Jacob" surname="Sayles" eventType="2020" timestamp="2000-01-01T00:11:57" /><hid:EventMessage readerAddress="0" rawCardNumber="01D609AD" eventType="1022" timestamp="2000-01-01T00:10:33" /><hid:EventMessage readerAddress="0" rawCardNumber="010F2B8E" eventType="1022" timestamp="2000-01-01T00:10:25" /><hid:EventMessage readerAddress="0" rawCardNumber="009B4C9B" eventType="1022" timestamp="2000-01-01T00:10:20" /><hid:EventMessage readerAddress="0" commandStatus="true" eventType="12032" timestamp="2000-01-01T00:10:14" /><hid:EventMessage readerAddress="0" ioState="0" eventType="4034" timestamp="2000-01-01T00:03:14" /><hid:EventMessage readerAddress="0" ioState="0" eventType="4034" timestamp="2000-01-01T00:00:52" /><hid:EventMessage readerAddress="0" ioState="0" eventType="4034" timestamp="2000-01-07T14:40:29" /></hid:EventMessages></VertXMessage>

###############################################################################################################
# RoleSet Commands
###############################################################################################################

# <hid:RoleSet action="UD" roleSetID="1">
#     <hid:Roles>
#         <hid:Role roleID="1" scheduleID="1" resourceID="0"/>
#     </hid:Roles>
# </hid:RoleSet>

def add_roleset(roleID):
    # This adds a basic schedule and rollset that equal "24x7" on a factory system
    root = root_elm()
    rollset = ElementTree.SubElement(root, 'hid:RoleSet')
    rollset.set('action', 'UD')
    rollset.set('roleSetID', roleID)
    rolls = ElementTree.SubElement(rollset, 'hid:Roles')
    roll = ElementTree.SubElement(rolls, 'hid:Role')
    roll.set('roleID', roleID)
    roll.set('scheduleID', '1')
    roll.set('resourceID', '0')
    return root


###############################################################################################################
# Schedule Commands
###############################################################################################################


def schedule_elm(action):
    root = root_elm()
    elm = ElementTree.SubElement(root, 'hid:Schedules')
    elm.set('action', action)
    return (root, elm)
    
# <hid:Schedules action="LR" recordOffset="0" recordCount="10"/>
def list_schedules(recordOffset, recordCount):
    root, elm = schedule_elm('LR')
    elm.set('recordOffset', str(recordOffset))
    elm.set('recordCount', str(recordCount))
    return root

# <hid:Schedules action="UD" recordOffset="0" recordCount="10"/>
def assign_schedule():
    pass

def remove_schedule():
    pass


###############################################################################################################
# System Commands
###############################################################################################################


# <hid:Time action="UD"
#           month="6"
#           dayOfMonth="26"
#           year="2015"
#           hour="19"
#           minute="37"
#           second="00"
#           TZ="CST6CDT,M3.2.0/2,M11.1.0/2"
#           TZCode="062" />
def set_time(current_time=None):
    if current_time == None:
        current_time = datetime.now()
    root = root_elm()
    elm = ElementTree.SubElement(root, 'hid:Time')
    elm.set('action', 'UD')
    elm.set('year', str(current_time.year))
    elm.set('month', str(current_time.month))
    elm.set('dayOfMonth', str(current_time.day))
    elm.set('hour', str(current_time.hour))
    elm.set('minute', str(current_time.minute))
    elm.set('second', str(current_time.second))
    return root

# <hid:System action="CM" command="restartNetwork"/>
def restart_network():
    root = root_elm()
    elm = ElementTree.SubElement(root, 'hid:System')
    elm.set('action', 'CM')
    elm.set('command', 'restartNetwork')
    return root
