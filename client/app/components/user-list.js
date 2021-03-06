import _ from 'lodash';

const UserListComponent = {
    bindings: {
        ngModel: '='
    },
    controller: function (dataService) {
        var vm = this;
        vm.addUser = addUser;
        vm.removeUser = (user) => _.pull(vm.ngModel, user);
        vm.searchText = '';
        vm.searchUser = searchUser;

        //// functions

        function addUser() {
            if(vm.selectedItem) {
                vm.ngModel.push(angular.copy(vm.selectedItem));
            }
            vm.selectedItem = undefined;
        }

        function capitalize(text) {
            return text.charAt(0).toUpperCase() + text.slice(1);
        }

        function searchUser(searchName) {
            return dataService.searchUser(capitalize(searchName)).then((response) => {
                return response.data.query.globalallusers;
            });
        }
    },
    template: `<div>
        <span layout="row" layout-align="space-between center" ng-repeat="user in $ctrl.ngModel">
            <span>{{user.name}}</span>
            <span flex></span>
            <md-icon class="link" ng-click="$ctrl.removeUser(user)">close</md-icon>
        </span>
        <md-autocomplete flex
            md-input-name="autocomplete"
            md-input-minlength="2"
            md-input-maxlength="100"
            md-autoselect="true"
            md-selected-item-change="$ctrl.addUser()"
            md-selected-item="$ctrl.selectedItem"
            md-search-text="$ctrl.searchText"
            md-items="item in $ctrl.searchUser($ctrl.searchText)"
            md-item-text="item.name"
            md-floating-label="Enter username">
          <md-item-template>
            <span md-highlight-text="$ctrl.searchText">{{item.name}}</span>
          </md-item-template>
        </md-autocomplete>
    </div>`
};

export default () => {
    angular
        .module('montage')
        .component('montUserList', UserListComponent);
};
