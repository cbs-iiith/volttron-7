'use strict';

var React = require('react');
var ReactDOM = require('react-dom');

import { Router, Route, IndexRedirect, hashHistory } from 'react-router';

var authorizationStore = require('./stores/authorization-store');
var platformsPanelItemsStore = require('./stores/platforms-panel-items-store');
var devicesStore = require('./stores/devices-store');
var Dashboard = require('./components/dashboard');
var LoginForm = require('./components/login-form');
var PageNotFound = require('./components/page-not-found');
var Platform = require('./components/platform');
import PlatformManager from './components/platform-manager';
var Platforms = require('./components/platforms');
// var Devices = require('./components/devices');
import ConfigureDevices from './components/configure-devices';
var PlatformCharts = require('./components/platform-charts');
var Navigation = require('./components/navigation');

var _afterLoginPath = '/dashboard';


const checkAuth = AuthComponent => class extends React.Component {
    componentWillMount() {

        if ((AuthComponent.displayName !== 'LoginForm') && (AuthComponent.displayName !== 'PageNotFound')) {
            if (!authorizationStore.getAuthorization()) {
                hashHistory.replace('/login');
            }
        } 
        else if (authorizationStore.getAuthorization()) {
            hashHistory.replace(_afterLoginPath);
        }
    }

    render() {
        return <AuthComponent {...this.props}/>;
    }
};

var PublicExterior = React.createClass({
    render: function() {

        return (
            <div className="public-exterior not-logged-in">
                <div className="main">
                    <Navigation />
                    {this.props.children}
                </div>
            </div>
        );
    }
});

var routes = (
    <Router history={hashHistory}>
        <Route path="/" component={checkAuth(PlatformManager)} > 
            <IndexRedirect to="dashboard" />
            <Route path="dashboard" component={checkAuth(Dashboard)} />
            <Route path="platforms" component={checkAuth(Platforms)} />
            <Route path="platform/:uuid" component={checkAuth(Platform)} />
            <Route path="configure-devices" component={checkAuth(ConfigureDevices)} />
            <Route path="charts" component={checkAuth(PlatformCharts)} />
        </Route>
        <Route path="/" component={checkAuth(PublicExterior)} > 
            <Route path="login" component={checkAuth(LoginForm)} />
        </Route>
        <Route path="*" component={PageNotFound}/>
        
    </Router>
);

ReactDOM.render(routes, document.getElementById('app'), function (Handler) {
    authorizationStore.addChangeListener(function () {
        if (authorizationStore.getAuthorization() && this.router.isActive('/login')) 
        {
            this.router.replace(_afterLoginPath);
        } 
        else if (!authorizationStore.getAuthorization() && !this.router.isActive('/login')) 
        {
            this.router.replace('/login');
        }
    }.bind(this));

    platformsPanelItemsStore.addChangeListener(function () {
        if (platformsPanelItemsStore.getLastCheck() && authorizationStore.getAuthorization())
        {
            if (!this.router.isActive('charts'))
            {
                this.router.push('/charts');
            }
        }

    }.bind(this));

    devicesStore.addChangeListener(function () { 
        if (devicesStore.getNewScan())       
        {
            if (!this.router.isActive('configure-devices'))
            {
                this.router.push('/configure-devices');
            }
        }
    }.bind(this));
});



