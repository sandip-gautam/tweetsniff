#!/usr/bin/env python
#
# Author: Xavier Mertens <xavier@rootshell.be>
# Copyright: GPLv3 (http://gplv3.fsf.org/)
# Feel free to use the code, but please share the changes you've made
# 
import argparse
import errno
import ConfigParser
import json
import logging
import logging.handlers
import os
import re
import signal
import sys
import time

try:
	import twitter
except:
	print "[ERROR]: python-twitter is required. See https://github.com/bear/python-twitter"

try:
	import syslog
except:
	print "[INFO]: No Syslog support, logging to console"
from datetime import datetime
from dateutil import parser
from dateutil import tz
from elasticsearch import Elasticsearch
from termcolor import colored

import hashlib
import urllib, httplib

api = None
logger = None

# Default configuration 
config = {
	'statusFile': '/var/run/tweetsniff.status',
	'esServer': '',
	'keywords': '',
	'regex': '',
	'highlightColor': 'red',
	'keywordColor': 'blue',
	'cefServer': '',
	'cefPort': ''
}

def sigHandler(s, f):

	"""Cleanup once CTRL-C is received"""

	print "Killed."
	sys.exit(0)

def writeLog(msg):

	"""Output a message to the console/Syslog depending on the host"""

	if os.name == "posix":
        	syslog.openlog(logoption=syslog.LOG_PID,facility=syslog.LOG_MAIL)
        	syslog.syslog(msg)
	else:
		print msg
        return

def writeCEFEvent(tweet):

	"""Send a CEF event to a Syslog destination"""

	# print "[Debug]: Writing CEF: %s" % tweet
	cefmsg = ' CEF:0|blog.rootshell.be|tweetsniff|1.0|TwitterMsg|Received Twitter Message|0|cs1Label=TweetHandle cs1=%s cs2Label=TweetTime cs2=%s msg=%s' % (tweet.user.screen_name, tweet.created_at, tweet.text)
	logger.info(cefmsg)
	return

def time2Local(s):

	"""Convert a 'created_at' date (UTC) to local time"""

	if not s:
		utc = datetime.utcnow()
	else:
		utc = datetime.strptime(parser.parse(s).strftime('%Y-%m-%d %H:%M:%S'), '%Y-%m-%d %H:%M:%S')

	from_zone = tz.tzutc()
	to_zone = tz.tzlocal()
	utc = utc.replace(tzinfo=from_zone)
	return(utc.astimezone(to_zone))

def indexEs(tweet, urls, tweet_message):

	"""Index a new Tweet in Elasticsearch"""

	doc = tweet.AsDict()
	# Delete 'retweeted_status' - to be fixed later
	if 'retweeted_status' in doc:
		del doc['retweeted_status']	
	# Delete old urls'
	if 'urls' in doc:
		del doc['urls']

	# To fix: support different timezones? (+00:00
	try:
		doc['@timestamp'] = parser.parse(doc['created_at']).strftime("%Y-%m-%dT%H:%M:%S+00:00")
		doc['text'] = tweet_message

		if config['urls_process_urls'] == True:
			if urls:
				x = 1
				for url in urls:
					doc['urls.' + str(x) + '.url'] = url['url']
					doc['urls.' + str(x) + '.url_unshortened'] = url['url_unshortened']
					doc['urls.' + str(x) + '.url_md5'] = url['url_md5']
					doc['urls.' + str(x) + '.url_sha1'] = url['url_sha1']
					x = x + 1
		res = es.index(index=time.strftime(esIndex, time.localtime()),
				doc_type='tweet',
				body=doc)
	except:
	    print "[Warning] Can't connect to %s" % config['esServer']

	return

def processURL(urls):

	""" Process the short URLs to get expanded and get their hashes """

	processed_urls = []
	if urls:
		for url in urls:
			url_unshortened = unshortenURL(url.expanded_url.encode("utf-8"))			
			url_md5 = hashlib.md5(url_unshortened.encode('utf-8')).hexdigest()
			url_sha1 = hashlib.sha1(url_unshortened.encode('utf-8')).hexdigest()
			processed_urls.append( { 	'url': url.url.encode('utf-8'), 
							'url_unshortened': url_unshortened, 
							'url_md5': url_md5, 
							'url_sha1': url_sha1 })
	return processed_urls

def unshortenURL(url):

	""" unshortenURL  https://github.com/cudeso/expandurl"""

	url_ua = config['urls_ua']
	urls_timeout = 	config['urls_timeout']
	currenturl = url.strip()
	previousurl = None
	while currenturl != previousurl:

	    try:
	        httprequest = httplib.urlsplit(currenturl)
	        scheme = httprequest.scheme.lower()
	        netloc = httprequest.netloc.lower()
	        previousurl = currenturl
	        if scheme == 'http':
	            conn = httplib.HTTPConnection(netloc, timeout=5)
	            req = currenturl[7+len(netloc):]
	            location = "%s://%s" % (scheme, netloc)
	        elif scheme=='https':
	            conn = httplib.HTTPSConnection(netloc, timeout=5)
	            req = currenturl[8+len(netloc):]
	            location = "%s://%s" % (scheme, netloc)           

	        conn.request("HEAD", req, None, {'User-Agent': url_ua,'Accept': '*/*',})
	        res = conn.getresponse()

	        if res.status in [301, 304]:
	            currenturl = res.getheader('Location')
	            httprequest_redirect = httplib.urlsplit(currenturl)

	            if httprequest_redirect.scheme.lower() != 'http' and httprequest_redirect.scheme.lower() != 'https':
	                # currenturl does not contain http(s) 
	                currenturl = "%s://%s%s" % (scheme, netloc,currenturl)
	    except:
	        currenturl = url

	return currenturl

def updateTimeline(timeline_id):

	"""Get new Tweets from twitter.com"""

	try:
		timeline = api.GetHomeTimeline(since_id=timeline_id)
	except twitter.error.TwitterError as e:
		print "[Error] Twitter returned: %s (%d)" % (e[0][0]['message'], e[0][0]['code'])
		return timeline_id

	if not timeline:
		return timeline_id

	last_id = timeline_id
	for t in reversed(timeline):
		text = t.text
		for r in config['regex']:
			if r:
				if re.search('('+r+')', text, re.I):
					text = text.replace(r, colored(r, config['highlightColor']))

		tweet_message = text.encode("utf-8")
		if config['urls_process_urls'] == True:
			urls = processURL(t.urls)
			for url in urls:
				tweet_message = tweet_message.replace( url['url'] , url['url_unshortened'])
		else:
			urls = []

		print "%s | %15s | %s" % (time2Local(t.created_at).strftime("%H:%M:%S"),
					t.user.screen_name.encode("utf-8"),
					tweet_message)

		if es:
			indexEs(t, urls, tweet_message)

		if logger:
			writeCEFEvent(t)

		if (long(t.id) > long(last_id)):
			last_id = t.id
	return(last_id)

def updateSearch(search_id):

	"""Get new Tweets containing specific keywords"""

	last_id = search_id
	for keyword in config['keywords']:
		if not keyword:
			continue
		try:
			tweets = api.GetSearch(term=keyword, since_id=search_id)
		except twitter.error.TwitterError as e:
			print "[Error] Twitter returned: %s (%s)" % (e[0][0]['message'], str(e[0][0]['code']))
			return(search_id)

		if not tweets:
			continue

		for t in reversed(tweets):
			text = t.text

			# Highlight keyword
			if re.search('('+keyword+')', text, re.I):
				text = text.replace(keyword, colored(keyword, config['keywordColor']))

			for r in config['regex']:
				if r:
					if re.search('('+r+')', text, re.I):
						text = text.replace(r, colored(r, config['highlightColor']))

			tweet_message = text.encode("utf-8")
			if config['urls_process_urls'] == True:
				urls = processURL(t.urls)
				for url in urls:
					tweet_message = tweet_message.replace( url['url'] , url['url_unshortened'])
			else:
				urls = []

			print "%s | %15s | %s" % (time2Local(t.created_at).strftime("%H:%M:%S"),
						t.user.screen_name.encode("utf-8"),
						tweet_message)

			if es: 
				indexEs(t, urls, tweet_message)

			if logger:
				writeCEFEvent(t)

			if long(t.id) > long(last_id):
				last_id = t.id
	print "[DEBUG] last_id = %s" % last_id
	return(last_id)
	
def main():
	global api
	global config
	global es
	global esIndex
	global logger

	signal.signal(signal.SIGINT, sigHandler)

	parser = argparse.ArgumentParser(
		description='Display a Tweet feed')
	parser.add_argument('-c', '--config',
		dest = 'configFile',
		help = 'configuration file (default: /etc/tweetsniff.conf)',
		metavar = 'CONFIG')
	args = parser.parse_args()

	if not args.configFile:
		args.configFile = '/etc/tweetsniff.conf'

	try:
		c = ConfigParser.ConfigParser()
		c.read(args.configFile)
		# Twitter config
		consumerKey = c.get('twitterapi', 'consumer_key')
		consumerSecret = c.get('twitterapi', 'consumer_secret')
		accessTokenKey = c.get('twitterapi', 'access_token_key')
		accessTokenSecret = c.get('twitterapi', 'access_token_secret')
		config['statusFile'] = c.get('twitterapi', 'status_file')
		#Highligts
		config['highlightColor'] = c.get('highlight', 'color')
		highlightRegex = c.get('highlight', 'regex')
		# Search
		searchKeywords = c.get('search', 'keywords')
		config['keywordColor'] = c.get('search', 'color')
		# URLs
		config['urls_ua'] = c.get('urls', 'ua')
		config['urls_timeout'] = c.get('urls', 'timeout')
		config['urls_process_urls'] = c.get('urls', 'process_urls')
		if config['urls_process_urls'].lower() == 'true':
			config['urls_process_urls'] = True
		# Elasticsearch config (optional)
		config['esServer'] = c.get('elasticsearch', 'server')
		esIndex = c.get('elasticsearch', 'index')
		# CEF confit
		try:
			config['cefServer'] = c.get('cef', 'server')
			config['cefPort'] = c.get('cef', 'port')
		except:
			pass
	except OSError as e:
		writeLog('Cannot read config file %s: %s' % (args.configFile, e.errno()))
		exit

	print "DEBUG: %s, %s, %s, %s" % (consumerKey,consumerSecret,accessTokenKey,accessTokenSecret)
	print "DEBUG: Regex: %s" % highlightRegex

	if searchKeywords:
		config['keywords'] = searchKeywords.split('\n')
		print "DEBUG: keywords = %s" % config['keywords']

	if highlightRegex:
		config['regex'] = highlightRegex.split('\n')

	try:
		api = twitter.Api(consumer_key = consumerKey,
			consumer_secret = consumerSecret,
			access_token_key = accessTokenKey,
			access_token_secret = accessTokenSecret)
	except:
		print "[Error] Can't connect to twitter.com" 
		sys.exit(1)

	if config['esServer']:
		try:
			es = Elasticsearch(
				[config['esServer']]
				)
		except:
			print "[Warning] Can't connect to %s" % config['esServer']

	if config['cefServer']:
		try:
			logger = logging.getLogger('tweetsniff')
			logger.setLevel(logging.INFO)
			if config['cefPort']:
				handler = logging.handlers.SysLogHandler(address=(config['cefServer'], int(config['cefPort'])))
			else:
				handler = logging.handlers.SysLogHandler(address=(config['cefServer'], 514))
			logger.addHandler(handler)
		except:
			print "[Warning] Can't configure CEF destination: %s:%s", (config['cefServer'],config['cefPort'])

	if not os.path.isfile(config['statusFile']):
		print "DEBUG: Status file not found, starting new feed"
		timeline_id = 0
		search_id = 0
		
	else:
		fd = open(config['statusFile'], 'r')
                data = fd.read().split(',')
		timeline_id = data[0]
		search_id = data[1]
                fd.close()
		print "DEBUG: Restarting feed from ID %s/%s" % (timeline_id, search_id)

	while 1:
		try:
			timeline_id = updateTimeline(timeline_id)
			search_id = updateSearch(search_id)
		except AttributeError:
			print "[Error] Can't connect to twitter.com" 
			sys.exit(1)		

		fd = open(config['statusFile'], 'w')
		fd.write("%s,%s" % (str(timeline_id), str(search_id)))
		fd.close()
		try:
			sleep_home = api.GetAverageSleepTime('statuses/home_timeline')
			sleep_search = api.GetAverageSleepTime('search/tweets')
		except twitter.error.TwitterError as e:
			print "[Error] Twitter returned: %s (%s)" % (e[0][0]['message'], str(e[0][0]['code']))

		print "DEBUG: Sleep = %s / %s" % (sleep_home, sleep_search)
		if sleep_search > sleep_home:
			time.sleep(sleep_search)
		else:
			time.sleep(sleep_home)

if __name__ == '__main__':
	main()
