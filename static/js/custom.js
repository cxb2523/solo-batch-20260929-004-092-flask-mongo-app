$(document).ready(function() {
    console.log("Ready!");


    // Manage edit-name-button text
    $('#edit-name').on('hide.bs.collapse', function() {
        $('a.edit-name-button').html("Edit");
    });
    $('#edit-name').on('show.bs.collapse', function() {
        $('a.edit-name-button').html("Close");
    });


    // Validate password
    $("#submit-button").click(function(event) {
        var pass = $("#pass").val();
        var confirmation = $("#confirmation").val();
        var regex = /^(?=.*[0-9])(?=.*[!@#$%^&*])[a-zA-Z0-9!@#$%^&*]{8,32}$/;
        if (!pass.match(regex)) {
            event.preventDefault();
            alert("Password must be 8-32 characters long and contain at least one number and one special character.");
        } else if (pass != confirmation) {
            event.preventDefault();
            alert("Password and confirmation do not match. Please re-enter them and try again.");
        }
    });


    // Resend validation email
    $("a.verify-button").click(function(event) {
        event.preventDefault();
        $(this).html("Sending email");
        $(this).css('pointer-events', 'none');
        $(this).addClass("text-muted");
        $.post("/verify", $("form.verify-form").serialize())
            .done(function(response) {
                console.log(response);
                $("a.verify-button").html("Email sent");
            })
            .fail(function(response) {
                console.log(response)
            });
    });


    // Add note
    $("button.add-note-button").click(function() {
        $.post("/add_note", $("form.add-note-form").serialize())
            .done(function(response) {
                console.log(response);
                location.reload(true);
            })
            .fail(function(response) {
                console.log(response)
            });
    });


    // Delete note
    $("button.delete-note-button").click(function() {
        $.post("/delete_note", $("form.delete-note-form").serialize())
            .done(function(response) {
                console.log(response);
                location.reload(true);
            })
            .fail(function(response) {
                console.log(response)
            });
    });


    // Send message
    $("button.send-message-button").click(function() {
        $.post("/send_message", $("form.send-message-form").serialize())
            .done(function(response) {
                console.log(response);
                location.reload(true);
            })
            .fail(function(response) {
                console.log(response)
            });
    });


    // Delete message
    $("button.delete-message-button").click(function() {
        $.post("/delete_message", $("form.delete-message-form").serialize())
            .done(function(response) {
                console.log(response);
                location.reload(true);
            })
            .fail(function(response) {
                console.log(response)
            });
    });


    // Hide sent message
    $("button.hide-sent-message-button").click(function() {
        $.post("/hide_sent_message", $("form.hide-sent-message-form").serialize())
            .done(function(response) {
                console.log(response);
                location.reload(true);
            })
            .fail(function(response) {
                console.log(response)
            });
    });


    // Change Name
    $("button.change-name-button").click(function() {
        $.post("/change_name", $("form.change-name-form").serialize())
            .done(function(response) {
                console.log(response);
                location.reload(true);
            })
            .fail(function(response) {
                console.log(response)
            });
    });


    // Delete account
    $("button.second-delete").click(function(event) {
        if (confirm("Are you sure you want to delete your account?")) {
            console.log("Deleting account");
            $.post("/delete_account", $("form.delete-account-form").serialize())
                .done(function(response) {
                    console.log(response);
                    location.href = "/";
                })
                .fail(function(response) {
                    console.log(response)
                });
        } else {
            event.preventDefault();
            $("div.delete-account").collapse("hide");
        }
    });


    // Toggle delete button
    $("div.delete-account").on('hide.bs.collapse', function() {
        $("div.account-card").removeClass("border-danger");
        $("div.account-card").addClass("border-secondary");
        $("p.delete-warning").removeClass("text-danger");
        $("button.first-delete").html("Delete account");
    });
    $("div.delete-account").on('show.bs.collapse', function() {
        $("div.account-card").removeClass("border-secondary");
        $("div.account-card").addClass("border-danger");
        $("p.delete-warning").addClass("text-danger");
        $("button.first-delete").html("Hide the red button!");
    });


    // Follow / unfollow buttons (AJAX with full form fallback: without JS the
    // form simply POSTs and the view redirects back to the profile page).
    $("div.follow-controls").on('click', 'button.follow-button', function(event) {
        event.preventDefault();
        var $controls = $("div.follow-controls");
        var $form = $(this).closest('form');
        var $error = $controls.find('.follow-error');
        $error.attr('hidden', true);
        $.ajax({
            type: 'POST',
            url: $form.attr('action'),
            data: $form.serialize(),
            headers: {'X-Requested-With': 'XMLHttpRequest'}
        }).done(function(response) {
            console.log(response);
            // Swap the visible button without reloading the page.
            $controls.attr('data-relationship', response.state);
        }).fail(function(response) {
            console.log(response);
            var message = (response.responseJSON && response.responseJSON.error) ||
                'Could not update follow status.';
            $error.text(message).removeAttr('hidden');
        });
    });


    // Approve / deny pending follow requests.
    $(".approve-button, .deny-button").click(function(event) {
        event.preventDefault();
        var $button = $(this);
        var $form = $button.closest('form');
        $.ajax({
            type: 'POST',
            url: $form.attr('action'),
            data: $form.serialize(),
            headers: {'X-Requested-With': 'XMLHttpRequest'}
        }).done(function() {
            console.log('follow request updated');
            $button.closest('.pending-row').remove();
        }).fail(function(response) {
            console.log(response);
        });
    });


    // Toggle private account flag.
    $(".toggle-private-button").click(function(event) {
        event.preventDefault();
        var $form = $(this).closest('form');
        $.post($form.attr('action'), $form.serialize())
            .done(function() { location.reload(true); })
            .fail(function(response) { console.log(response); });
    });

});
